"""LiveExecutor: shared order lifecycle and ledger booking with real venue fills."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ExecutionMode
from app.core.clock import Clock
from app.db.models import Order, OrderStatus
from app.execution.orders import BookSource, ExecutionRequest, ObtainedFill
from app.execution.paper import PaperExecutor
from app.integrations.polymarket.live import (
    LiveGate,
    LiveOrderClient,
    LiveTradingUnavailable,
    fill_from_exchange,
    place_live_order,
)
from app.services.ledger import PortfolioLedger

log = logging.getLogger(__name__)

ClientFactory = Callable[[], Awaitable[LiveOrderClient]]

ORDER_GEOBLOCK_MAX_AGE = timedelta(seconds=90)
"""An order may reuse a geoblock result this old. The upstream-health worker refreshes it
every 60 s, so the 120-430 ms HTTPS check stays off the order path."""


class LiveExecutor(PaperExecutor):
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        ledger: PortfolioLedger,
        books: BookSource,
        clock: Clock,
        *,
        gate: LiveGate,
        client_factory: ClientFactory,
    ) -> None:
        super().__init__(sm, ledger, books, clock, mode=ExecutionMode.LIVE)
        self._gate = gate
        self._client_factory = client_factory
        self._client: LiveOrderClient | None = None
        self._uncertain = False

    async def recover_pending(self) -> int:
        async with self._sm() as session:
            pending = await session.scalar(
                select(Order.id)
                .where(
                    Order.mode == ExecutionMode.LIVE.value,
                    Order.status == OrderStatus.PENDING,
                )
                .limit(1)
            )
        if pending is not None:
            self._uncertain = True
            raise LiveTradingUnavailable(
                "Unreconciled live order: reservations retained; "
                "reconcile venue history before restart"
            )
        return 0

    async def _existing(self, key: str):
        async with self._sm() as session:
            order = await session.scalar(select(Order).where(Order.idempotency_key == key))
        if order is not None and order.status == OrderStatus.PENDING:
            self._uncertain = True
            raise LiveTradingUnavailable("Unreconciled live order; reservation retained")
        return await super()._existing(key)

    async def _obtain_fill(
        self, req: ExecutionRequest, delay_ms: int, slippage_bps: int, share_step: Decimal
    ) -> ObtainedFill | str:
        if self._uncertain:
            raise LiveTradingUnavailable("Live execution halted: uncertain venue outcome")
        status = await self._gate.check(max_age=ORDER_GEOBLOCK_MAX_AGE)
        if not status.live_enabled:
            log.warning("live order refused", extra={"reason": status.reason})
            return f"LIVE_DISABLED: {status.reason}"
        if self._client is None:
            self._client = await self._client_factory()
        try:
            resp = await place_live_order(
                self._client,
                token_id=req.token_id,
                side=req.side,
                budget=req.budget,
                shares=req.target_shares,
                limit_price=req.limit_price,
            )
        except Exception as exc:
            # A timeout is not evidence of rejection. Keep the persisted PENDING order
            # and its cash reservation, and refuse further exchange submissions.
            self._uncertain = True
            raise LiveTradingUnavailable(
                "Uncertain exchange submission; reconciliation required"
            ) from exc
        if not getattr(resp, "ok", False):
            return f"EXCHANGE_REJECTED: {getattr(resp, 'error', None) or resp!r}"[:300]
        if str(getattr(resp, "status", "")).lower() != "matched":
            self._uncertain = True
            raise LiveTradingUnavailable(
                "Exchange order not confirmed matched; reservation retained"
            )
        fill = fill_from_exchange(req.side, resp)
        if fill is None:
            return "NO_FILL"
        return ObtainedFill(fill, getattr(resp, "order_id", None), None, None)


async def sdk_client_factory(private_key: str, funder: str) -> LiveOrderClient:
    """Build an authenticated ``polymarket-client`` secure client (only when gated in)."""
    from polymarket import AsyncSecureClient

    return await AsyncSecureClient.create(private_key=private_key, wallet=funder)  # type: ignore[misc]
