"""Read model of our account: ledger state marked to current books."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.clock import Clock
from app.core.money import ZERO
from app.services.ledger import PortfolioLedger, PortfolioView
from app.services.market_data import MarketDataService


class PortfolioReader:
    def __init__(
        self, ledger: PortfolioLedger, market_data: MarketDataService, clock: Clock, account_id: int
    ) -> None:
        self._ledger = ledger
        self._md = market_data
        self._clock = clock
        self.account_id = account_id

    async def view(self) -> PortfolioView:
        lots = await self._ledger.open_lots(self.account_id)
        marks = await self._md.marks(lt.token_id for lt in lots)
        return await self._ledger.portfolio(self.account_id, {t: m.price for t, m in marks.items()})

    async def day_pnl(self, view: PortfolioView) -> Decimal:
        now = self._clock.now()
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        start_equity = await self._ledger.day_start_equity(self.account_id, day_start)
        return ZERO if start_equity is None else view.equity - start_equity

    async def view_with_day_pnl(self) -> tuple[PortfolioView, Decimal]:
        view = await self.view()
        return view, await self.day_pnl(view)
