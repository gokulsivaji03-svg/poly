"""Read model of our account: ledger state marked to current books."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from app.core.clock import Clock
from app.core.money import ZERO
from app.services.ledger import (
    LedgerSnapshot,
    PortfolioLedger,
    PortfolioView,
    build_portfolio_view,
)
from app.services.market_data import MarketDataService

log = logging.getLogger(__name__)


class PortfolioReader:
    def __init__(
        self, ledger: PortfolioLedger, market_data: MarketDataService, clock: Clock, account_id: int
    ) -> None:
        self._ledger = ledger
        self._md = market_data
        self._clock = clock
        self.account_id = account_id
        self._day_start: tuple[datetime, Decimal] | None = None
        self._snap: LedgerSnapshot | None = None
        self._reload: asyncio.Task[None] | None = None

    async def snapshot(self) -> LedgerSnapshot:
        """The ledger rows as of now: one version read when nothing changed since the last
        load, a reload (shared with any already running) when something did."""
        version = await self._ledger.ledger_version(self.account_id)
        while self._snap is None or self._snap.version < version:
            await asyncio.shield(self._start_reload())
        return self._snap

    def refresh_soon(self) -> None:
        """Reload in the background after a write, so the next copy finds a current snapshot."""
        self._start_reload().add_done_callback(_log_failure)

    def _start_reload(self) -> asyncio.Task[None]:
        if self._reload is None:
            self._reload = asyncio.create_task(self._load())
        return self._reload

    async def _load(self) -> None:
        try:
            snap = await self._ledger.snapshot(self.account_id)
            if self._snap is None or snap.version > self._snap.version:
                self._snap = snap
        finally:
            self._reload = None

    async def view(self, snap: LedgerSnapshot | None = None) -> PortfolioView:
        snap = snap or await self.snapshot()
        marks = await self._md.marks(lt.token_id for lt in snap.lots)
        prices = {t: m.price for t, m in marks.items()}
        return build_portfolio_view(snap.account, snap.lots, prices, self._clock.now())

    async def day_pnl(self, view: PortfolioView) -> Decimal:
        start_equity = await self._day_start_equity()
        return ZERO if start_equity is None else view.equity - start_equity

    async def copy_inputs(self) -> tuple[LedgerSnapshot, PortfolioView, Decimal]:
        """The snapshot, its marked view and today's P&L: what a copy decision reads."""
        (snap, view), start_equity = await asyncio.gather(
            self._snapshot_and_view(), self._day_start_equity()
        )
        return snap, view, ZERO if start_equity is None else view.equity - start_equity

    async def _snapshot_and_view(self) -> tuple[LedgerSnapshot, PortfolioView]:
        snap = await self.snapshot()
        return snap, await self.view(snap)

    async def _day_start_equity(self) -> Decimal | None:
        """Equity at the start of the UTC day. Once found it is fixed for the day, so it is
        read from the database once per day instead of on every copy."""
        now = self._clock.now()
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        if self._day_start is not None and self._day_start[0] == day_start:
            return self._day_start[1]
        equity = await self._ledger.day_start_equity(self.account_id, day_start)
        if equity is not None:
            self._day_start = (day_start, equity)
        return equity


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("portfolio refresh failed", extra={"error": repr(exc)})
