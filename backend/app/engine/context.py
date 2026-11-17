"""Context loading: everything a copy decision reads, fetched concurrently in one place.

Strategy and risk are pure; this module is where their inputs are read from the database,
the market-data cache and the source-equity estimator. All reads for one decision run in a
single ``asyncio.gather`` so the decision costs one round trip of the slowest read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.money import ZERO
from app.db.models import CopySignal, LotOrigin, SignalStatus, SourceEvent, WatchedWallet
from app.domain.market import MarketInfo, OrderBook
from app.domain.settings import SizingSettings, StrategySettings
from app.engine.portfolio import PortfolioReader
from app.risk.pretrade import PortfolioSnapshot
from app.services.equity_estimator import WalletEquityEstimator
from app.services.market_data import MarketDataService
from app.services.settings_service import deep_merge
from app.strategy.copy import BuyContext, CopyLot, SellContext, SourceTrade

BATCHED = "BATCHED"
"""A copy below the minimum order, carried into this wallet's next copy of the token."""


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
    wallet_headroom: Decimal | None
    carried: Decimal
    """Budget of this wallet's earlier copies of this token that were too small to place."""


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
        sizing = effective_sizing(settings.sizing, wallet.sizing_override)
        (
            sold_after,
            market,
            (ledger, view, day_pnl),
            estimate,
            book,
            (followed, carried),
        ) = await asyncio.gather(
            self._source_sold_after(ev),
            self._md.get_market(ev.condition_id),
            self._portfolio.copy_inputs(),
            self._est.estimate_at(
                ev.wallet_id, wallet.address, ev.block_ts, exclude_event_id=ev.id
            ),
            self._md.get_book(ev.token_id),
            self._allotment(ev),
        )
        bankroll, headroom = view.equity, None
        if sizing.split_across_wallets:
            bankroll = view.equity / max(followed, 1)
            held = sum(
                (
                    lt.cost_remaining
                    for lt in ledger.lots
                    if lt.source_wallet == wallet.address and lt.origin == LotOrigin.COPY
                ),
                ZERO,
            )
            headroom = bankroll - held
        return BuyInputs(
            strategy=BuyContext(
                trade=source_trade(ev),
                market=market,
                equity=view.equity,
                bankroll=bankroll,
                source_equity=estimate,
                source_sold_after=sold_after,
                sizing=sizing,
            ),
            snapshot=view.risk_snapshot(day_pnl),
            book=book,
            buys_in_market=ledger.copy_buys.get(ev.condition_id, 0),
            wallet_headroom=headroom,
            carried=carried,
        )

    async def sell(self, ev: SourceEvent, wallet: WatchedWallet) -> SellInputs:
        ledger, after, book, market = await asyncio.gather(
            self._portfolio.snapshot(),
            self._est.source_position_size(wallet, ev.token_id),
            self._md.get_book(ev.token_id),
            self._md.get_market(ev.condition_id),
        )
        lots = tuple(
            CopyLot(lt.id, lt.shares_remaining, lt.matched_shares)
            for lt in ledger.lots
            if lt.token_id == ev.token_id
            and lt.source_wallet == wallet.address
            and lt.origin == LotOrigin.COPY
        )
        return SellInputs(SellContext(source_trade(ev), lots, after), book, market)

    def warm(self) -> None:
        """After an execution wrote the ledger: reload the snapshot the next copy reads."""
        self._portfolio.refresh_soon()

    async def _allotment(self, ev: SourceEvent) -> tuple[int, Decimal]:
        """Followed wallets, and the target budgets of this wallet's BATCHED copies of this
        token since the last filled copy buy or source sell of it, which either placed the
        carry or ended the position."""
        mine = (CopySignal.wallet_id == ev.wallet_id, CopySignal.token_id == ev.token_id)
        async with self._sm() as s:
            followed = await s.scalar(
                select(func.count())
                .select_from(WatchedWallet)
                .where(WatchedWallet.follow.is_(True), WatchedWallet.removed_at.is_(None))
            )
            since = await s.scalar(
                select(func.max(CopySignal.id)).where(
                    *mine,
                    or_(
                        CopySignal.side == "SELL",
                        CopySignal.status.in_([SignalStatus.EXECUTED, SignalStatus.PARTIAL]),
                    ),
                )
            )
            total = await s.scalar(
                select(func.sum(CopySignal.target_budget)).where(
                    *mine,
                    CopySignal.id > (since or 0),
                    cast(CopySignal.reasons, JSONB).contains({"items": [{"code": BATCHED}]}),
                )
            )
        return followed or 0, total or ZERO

    async def _source_sold_after(self, ev: SourceEvent) -> bool:
        """A SELL already seen when this BUY was detected. Never a later one: a replay holds
        the whole history, so without the bound it would read the source's future."""
        async with self._sm() as s:
            hit = await s.scalar(
                select(SourceEvent.id)
                .where(
                    SourceEvent.wallet_id == ev.wallet_id,
                    SourceEvent.token_id == ev.token_id,
                    SourceEvent.type == "TRADE",
                    SourceEvent.side == "SELL",
                    SourceEvent.block_ts > ev.block_ts,
                    SourceEvent.block_ts <= ev.detected_at,
                )
                .limit(1)
            )
        return hit is not None
