"""Order contracts shared by every producer (copy engine, hedge scanner) and every venue.

An ``ExecutionRequest`` is a fully risk-approved instruction; a venue turns it into an
``ExecutionResult`` and books the fill. Nothing upstream of execution touches a venue type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.core.money import ZERO
from app.db.models import LotOrigin, OrderKind, OrderStatus
from app.domain.fills import FillSimulation
from app.domain.market import FeeModel, OrderBook, Side


class DuplicateInFlight(RuntimeError):
    """The same idempotency key is already executing in this process."""


class BookSource(Protocol):
    async def get_book(self, token_id: str, *, fresh: bool = False) -> OrderBook | None: ...


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    account_id: int
    idempotency_key: str
    kind: OrderKind
    side: Side
    condition_id: str
    token_id: str
    fee_model: FeeModel
    max_book_age: timedelta
    budget: Decimal | None = None
    """BUY cash cap (must be <= reservation)."""
    target_shares: Decimal | None = None
    """BUY: target delivered shares (hedges). SELL: shares to sell."""
    limit_price: Decimal | None = None
    reservation_id: int | None = None
    origin: LotOrigin = LotOrigin.COPY
    outcome: str = ""
    question: str = ""
    source_wallet: str | None = None
    signal_id: int | None = None
    hedge_id: int | None = None
    allow_matched: bool = False
    lot_ids: tuple[int, ...] | None = None
    """SELL: restrict to these lots (e.g. only lots copied from one source wallet)."""
    min_order_usd: Decimal = ZERO
    match_allocations: tuple[tuple[int, Decimal], ...] | None = None
    """Hedge BUY: primary (lot_id, shares) to pair with the delivered shares, in priority."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    order_id: int
    status: OrderStatus
    fill: FillSimulation | None = None
    lot_id: int | None = None
    closure_ids: tuple[int, ...] = ()
    reason: str | None = None
    replayed: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True, slots=True)
class ObtainedFill:
    """What a venue (simulated or real) delivered for one order."""

    fill: FillSimulation
    book_hash: str | None
    book_received_at: datetime | None
    book_age_ms: int | None


class Executor(Protocol):
    """A venue: executes approved requests idempotently and books fills in the ledger."""

    async def execute(
        self,
        req: ExecutionRequest,
        *,
        delay_ms: int = 0,
        slippage_bps: int = 0,
        share_step: Decimal = Decimal("0.01"),
    ) -> ExecutionResult: ...

    async def recover_pending(self) -> int: ...
