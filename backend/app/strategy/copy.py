"""Copy strategy: what to do about one source trade. Pure: no I/O, no clock, no ledger.

``screen`` decides whether a trade is a copy candidate at all; ``decide_buy`` and
``decide_sell`` turn a loaded context into an intent (how much to buy or sell) or a terminal
``Skip``. Limits that protect the account belong to ``app.risk``, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.money import ONE, ZERO
from app.db.models import SignalStatus
from app.domain.equity import EquityEstimate
from app.domain.market import MarketInfo, OutcomeToken
from app.domain.reasons import Reason
from app.domain.settings import SizingSettings, StrategySettings
from app.strategy.sell_policy import plan_copy_sell
from app.strategy.sizing import size_copy

PAIR_UNWIND_REVIEW = "PAIR_UNWIND_REVIEW"


@dataclass(frozen=True, slots=True)
class SourceTrade:
    """The copy-relevant facts of one ingested source trade."""

    side: str
    condition_id: str
    token_id: str
    price: Decimal
    shares: Decimal
    cost: Decimal
    block_ts: datetime
    is_combo: bool = False


@dataclass(frozen=True, slots=True)
class Skip:
    """Terminal outcome before execution; ``record`` holds signal fields worth keeping."""

    status: SignalStatus
    reasons: tuple[Reason, ...]
    record: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BuyContext:
    trade: SourceTrade
    market: MarketInfo | None
    equity: Decimal
    """Our equity (cash + marked positions) right now."""
    source_equity: EquityEstimate
    source_sold_after: bool
    sizing: SizingSettings


@dataclass(frozen=True, slots=True)
class BuyIntent:
    budget: Decimal
    market: MarketInfo
    token: OutcomeToken
    reasons: tuple[Reason, ...]
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CopyLot:
    lot_id: int
    shares: Decimal
    matched_shares: Decimal


@dataclass(frozen=True, slots=True)
class SellContext:
    trade: SourceTrade
    lots: tuple[CopyLot, ...]
    """Our open COPY lots in this token copied from this wallet, oldest first."""
    source_position_after: Decimal | None
    """Source's holding after its sell; ``None`` when unobservable."""


@dataclass(frozen=True, slots=True)
class SellIntent:
    shares: Decimal
    lot_ids: tuple[int, ...]
    reasons: tuple[Reason, ...]
    record: dict[str, Any]


def screen(trade: SourceTrade, *, followed: bool, settings: StrategySettings) -> Reason | None:
    """Policy filter applied before any data is loaded."""
    if trade.is_combo:
        return Reason("COMBO_UNSUPPORTED", "Combo/parlay trades are not copied.")
    if not followed:
        return Reason("NOT_FOLLOWED", "Wallet is monitored but copying is off.")
    if not settings.copy_.enabled:
        return Reason("COPY_DISABLED", "Copy trading is disabled in settings.")
    if trade.side == "SELL" and not settings.copy_.copy_sells:
        return Reason("SELL_COPY_DISABLED", "Copying source sells is disabled.")
    if not trade.condition_id or not trade.token_id or not (ZERO < trade.price < ONE):
        return Reason("UNMAPPED_EVENT", "Event lacks market/token mapping or a valid price.")
    return None


def decide_buy(ctx: BuyContext) -> BuyIntent | Skip:
    if ctx.source_sold_after:
        # Late-indexed BUY: its later SELL was already handled, so a copy could never exit.
        return Skip(
            SignalStatus.SKIPPED,
            (Reason("SOURCE_SOLD_AFTER", "Source already sold this token after this buy."),),
        )
    market = ctx.market
    if market is None:
        return Skip(
            SignalStatus.REJECTED, (Reason("MARKET_UNAVAILABLE", "Market metadata unavailable."),)
        )
    token = market.token(ctx.trade.token_id)
    if token is None:
        return Skip(
            SignalStatus.REJECTED,
            (Reason("TOKEN_MISMATCH", "Traded token is not an outcome of this condition."),),
        )
    if market.closed or not market.accepting_orders:
        return Skip(
            SignalStatus.REJECTED, (Reason("MARKET_CLOSED", "Market is not accepting orders."),)
        )
    reasons: list[Reason] = []
    if not market.fees_known:
        reasons.append(Reason("FEES_ASSUMED", "Fee schedule unavailable; assumed 7% rate."))
    sizing = size_copy(
        source_trade_cost=ctx.trade.cost,
        source_equity=ctx.source_equity,
        user_equity=ctx.equity,
        settings=ctx.sizing,
    )
    reasons.extend(Reason("SIZING_NOTE", n) for n in sizing.notes)
    record: dict[str, Any] = {
        "source_equity": sizing.source_equity_used or ctx.source_equity.equity,
        "source_equity_coverage": ctx.source_equity.coverage.value,
        "source_confidence": ctx.source_equity.confidence,
        "source_alloc_pct": sizing.source_allocation_pct,
        "user_equity": ctx.equity,
        "proposed_pct": sizing.proposed_copy_pct,
        "target_budget": sizing.target_budget,
    }
    if sizing.skipped:
        skip = Reason(sizing.skip_code or "SIZING_SKIP", "; ".join(sizing.notes))
        return Skip(SignalStatus.SKIPPED, (*reasons, skip), record)
    return BuyIntent(sizing.target_budget, market, token, tuple(reasons), record)


def decide_sell(ctx: SellContext, settings: StrategySettings) -> SellIntent | Skip:
    total = sum((lt.shares for lt in ctx.lots), ZERO)
    matched = sum((lt.matched_shares for lt in ctx.lots), ZERO)
    after = ctx.source_position_after
    plan = plan_copy_sell(
        source_sell_shares=ctx.trade.shares,
        source_position_before=None if after is None else after + ctx.trade.shares,
        our_total_shares=total,
        our_matched_shares=matched,
        policy=settings.copy_.sell_policy,
        hedged_policy=settings.copy_.hedged_sell_policy,
        share_step=settings.paper.share_step,
    )
    reasons = [Reason(code, code.replace("_", " ").capitalize()) for code in plan.reasons]
    if plan.consider_pair_unwind:
        reasons.append(
            Reason(
                PAIR_UNWIND_REVIEW,
                "Hedge engine will evaluate unwinding matched pairs (UNWIND_IF_BETTER).",
            )
        )
    record: dict[str, Any] = {
        "target_shares": plan.shares_to_sell,
        "source_alloc_pct": plan.source_sell_fraction,
    }
    if plan.shares_to_sell <= ZERO:
        nothing = Reason("NOTHING_TO_SELL", "No unhedged copied inventory to reduce.")
        return Skip(SignalStatus.SKIPPED, tuple(reasons) or (nothing,), record)
    return SellIntent(
        plan.shares_to_sell, tuple(lt.lot_id for lt in ctx.lots), tuple(reasons), record
    )
