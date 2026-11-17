"""Read model of our account: ledger state marked to current books."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.core.clock import Clock
from app.core.money import ZERO
from app.services.ledger import PortfolioLedger, PortfolioView, build_portfolio_view
from app.services.market_data import MarketDataService


class PortfolioReader:
    def __init__(
        self, ledger: PortfolioLedger, market_data: MarketDataService, clock: Clock, account_id: int
    ) -> None:
        self._ledger = ledger
        self._md = market_data
        self._clock = clock
        self.account_id = account_id
        self._day_start: tuple[datetime, Decimal] | None = None

    async def view(self) -> PortfolioView:
        acct, lots = await self._ledger.account_with_lots(self.account_id)
        marks = await self._md.marks(lt.token_id for lt in lots)
        prices = {t: m.price for t, m in marks.items()}
        return build_portfolio_view(acct, lots, prices, self._clock.now())

    async def day_pnl(self, view: PortfolioView) -> Decimal:
        start_equity = await self._day_start_equity()
        return ZERO if start_equity is None else view.equity - start_equity

    async def view_with_day_pnl(self) -> tuple[PortfolioView, Decimal]:
        view, start_equity = await asyncio.gather(self.view(), self._day_start_equity())
        return view, ZERO if start_equity is None else view.equity - start_equity

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
