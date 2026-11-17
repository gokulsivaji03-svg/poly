"""Context loading: everything a copy decision reads, fetched concurrently in one place.

Strategy and risk are pure; this module is where their inputs are read from the database,
the market-data cache and the source-equity estimator. All reads for one decision run in a
single ``asyncio.gather`` so the decision costs one round trip of the slowest read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.money import ZERO
from app.db.models import Lot, LotOrigin, Order, OrderKind, OrderStatus, SourceEvent, WatchedWallet
from app.domain.market import MarketInfo, OrderBook
from app.domain.settings import SizingSettings, StrategySettings
from app.engine.portfolio import PortfolioReader
from app.risk.pretrade import PortfolioSnapshot
from app.services.equity_estimator import WalletEquityEstimator
from app.services.market_data import MarketDataService
from app.services.settings_service import deep_merge
from app.strategy.copy import BuyContext, CopyLot, SellContext, SourceTrade


def source_trade(ev: SourceEvent) -> SourceTrade:
    return SourceTrade(
        side=ev.side or "",
        condition_id=ev.condition_id,
        token_id=ev.token_id,
        price=ev.price,
        shares=ev.size,
        cost=ev.usdc_size if ev.usdc_size > ZERO else ev.size * ev.price,
        block_ts=ev.block_ts,
        is_combo=ev.is_combo,
    )


def effective_sizing(base: SizingSettings, override: dict[str, Any] | None) -> SizingSettings:
    if not override:
        return base
    return SizingSettings.model_validate(deep_merge(base.model_dump(mode="json"), override))


@dataclass(frozen=True, slots=True)
class BuyInputs:
    strategy: BuyContext
    snapshot: PortfolioSnapshot
    book: OrderBook | None
    buys_in_market: int


@dataclass(frozen=True, slots=True)
class SellInputs:
    strategy: SellContext
    book: OrderBook | None
    market: MarketInfo | None


class ContextLoader:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        market_data: MarketDataService,
        estimator: WalletEquityEstimator,
        portfolio: PortfolioReader,
    ) -> None:
        self._sm = sm
        self._md = market_data
        self._est = estimator
        self._portfolio = portfolio

    async def buy(
        self, ev: SourceEvent, wallet: WatchedWallet, settings: StrategySettings
    ) -> BuyInputs:
        sold_after, market, (view, day_pnl), estimate, book, buys = await asyncio.gather(
            self._source_sold_after(ev),
            self._md.get_market(ev.condition_id),
            self._portfolio.view_with_day_pnl(),
            self._est.estimate_at(
                ev.wallet_id, wallet.address, ev.block_ts, exclude_event_id=ev.id
            ),
            self._md.get_book(ev.token_id),
            self._buys_in_market(ev.condition_id),
        )
        return BuyInputs(
            strategy=BuyContext(
                trade=source_trade(ev),
                market=market,
                equity=view.equity,
                source_equity=estimate,
                source_sold_after=sold_after,
                sizing=effective_sizing(settings.sizing, wallet.sizing_override),
            ),
            snapshot=view.risk_snapshot(day_pnl),
            book=book,
            buys_in_market=buys,
        )

    async def sell(self, ev: SourceEvent, wallet: WatchedWallet) -> SellInputs:
        lots, after, book, market = await asyncio.gather(
            self._copy_lots(ev.token_id, wallet.address),
            self._est.source_position_size(wallet, ev.token_id),
            self._md.get_book(ev.token_id),
            self._md.get_market(ev.condition_id),
        )
        return SellInputs(SellContext(source_trade(ev), lots, after), book, market)

    async def _source_sold_after(self, ev: SourceEvent) -> bool:
        async with self._sm() as s:
            hit = await s.scalar(
                select(SourceEvent.id)
                .where(
                    SourceEvent.wallet_id == ev.wallet_id,
                    SourceEvent.token_id == ev.token_id,
                    SourceEvent.type == "TRADE",
                    SourceEvent.side == "SELL",
                    SourceEvent.block_ts > ev.block_ts,
                )
                .limit(1)
            )
        return hit is not None

    async def _buys_in_market(self, condition_id: str) -> int:
        async with self._sm() as s:
            n = await s.scalar(
                select(func.count(Order.id)).where(
                    Order.account_id == self._portfolio.account_id,
                    Order.kind == OrderKind.COPY_BUY,
                    Order.condition_id == condition_id,
                    Order.status.in_([OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]),
                )
            )
        return n or 0

    async def _copy_lots(self, token_id: str, source_wallet: str) -> tuple[CopyLot, ...]:
        async with self._sm() as s:
            rows = (
                await s.scalars(
                    select(Lot)
                    .where(
                        Lot.account_id == self._portfolio.account_id,
                        Lot.token_id == token_id,
                        Lot.source_wallet == source_wallet,
                        Lot.origin == LotOrigin.COPY,
                        Lot.shares_remaining > 0,
                    )
                    .order_by(Lot.opened_at, Lot.id)
                )
            ).all()
        return tuple(CopyLot(lt.id, lt.shares_remaining, lt.matched_shares) for lt in rows)
