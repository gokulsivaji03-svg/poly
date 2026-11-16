"""Complementary-outcome hedge evaluation.

For matched binary outcome shares (one YES + one NO of the same condition pays exactly $1
at settlement, including 50/50 resolutions):

    gross_profit = matched * $1 - primary_cost_for_matched - hedge_purchase_cost
    net_profit   = gross_profit - attributable_fees - other_costs

Quotes are built from a multi-level order-book walk; a top-of-book price is never
extrapolated across the full quantity. Each included share must clear the configured
per-share gap on its *marginal* economics, so depth beyond the profitable levels is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.money import ONE, ZERO
from app.domain.fills import FillSimulation, simulate_buy
from app.domain.market import FeeModel, MarketInfo, OrderBook
from app.domain.settings import HedgeSettings


@dataclass(frozen=True, slots=True)
class HedgeLot:
    lot_id: int
    token_id: str
    unhedged_shares: Decimal
    """Net delivered shares not yet matched by an opposite-side lot."""
    cost_per_share: Decimal
    """All-in cash paid per delivered share (notional + cash fees)."""
    fee_per_share: Decimal = ZERO
    """Fee component (USDC) per delivered share, for disclosure."""


@dataclass(frozen=True, slots=True)
class LotAllocation:
    lot_id: int
    shares: Decimal
    primary_cost: Decimal
    primary_fee: Decimal


@dataclass(frozen=True, slots=True)
class HedgeQuote:
    condition_id: str
    primary_token_id: str
    hedge_token_id: str
    viable: bool
    reasons: tuple[str, ...]
    matched_shares: Decimal = ZERO
    total_unhedged: Decimal = ZERO
    allocations: tuple[LotAllocation, ...] = ()
    fill: FillSimulation | None = None
    primary_cost: Decimal = ZERO
    primary_fees: Decimal = ZERO
    hedge_cost: Decimal = ZERO
    """Total cash out for the hedge leg (notional + cash fees)."""
    hedge_fees: Decimal = ZERO
    other_costs: Decimal = ZERO
    payout: Decimal = ZERO
    net_profit: Decimal = ZERO
    gross_profit: Decimal = ZERO
    depth_usd: Decimal = ZERO
    best_ask: Decimal | None = None
    quoted_at: datetime | None = None
    book_received_at: datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def capital_required(self) -> Decimal:
        return self.hedge_cost

    @property
    def combined_capital(self) -> Decimal:
        return self.primary_cost + self.hedge_cost

    @property
    def net_per_share(self) -> Decimal:
        return self.net_profit / self.matched_shares if self.matched_shares > ZERO else ZERO

    @property
    def return_on_capital(self) -> Decimal:
        cap = self.combined_capital
        return self.net_profit / cap if cap > ZERO else ZERO

    @property
    def remaining_unmatched(self) -> Decimal:
        return self.total_unhedged - self.matched_shares

    @property
    def avg_hedge_price(self) -> Decimal | None:
        return self.fill.avg_price if self.fill else None

    @property
    def combined_cost_per_share(self) -> Decimal | None:
        if self.matched_shares <= ZERO:
            return None
        return self.combined_capital / self.matched_shares


def _no(cond: str, primary: str, hedge: str, *reasons: str, **kw: object) -> HedgeQuote:
    return HedgeQuote(cond, primary, hedge, False, tuple(reasons), **kw)  # type: ignore[arg-type]


def quote_hedge(
    *,
    market: MarketInfo,
    primary_token_id: str,
    lots: list[HedgeLot],
    book: OrderBook | None,
    fees: FeeModel,
    settings: HedgeSettings,
    now: datetime,
    max_book_age: timedelta,
    share_step: Decimal,
) -> HedgeQuote:
    complement = market.complement(primary_token_id)
    hedge_token = complement.token_id if complement else ""
    cond = market.condition_id
    lots = [lt for lt in lots if lt.unhedged_shares > ZERO]
    total_unhedged = sum((lt.unhedged_shares for lt in lots), ZERO)
    base: dict[str, object] = {"total_unhedged": total_unhedged, "quoted_at": now}

    if complement is None:
        return _no(cond, primary_token_id, hedge_token, "NOT_STANDARD_BINARY", **base)
    if market.neg_risk and not settings.allow_neg_risk:
        return _no(cond, primary_token_id, hedge_token, "NEG_RISK_UNSUPPORTED", **base)
    if market.closed or market.resolved or not market.accepting_orders:
        return _no(cond, primary_token_id, hedge_token, "MARKET_NOT_TRADING", **base)
    if any(lt.token_id != primary_token_id for lt in lots):
        return _no(cond, primary_token_id, hedge_token, "LOT_TOKEN_MISMATCH", **base)
    if not lots:
        return _no(cond, primary_token_id, hedge_token, "NOTHING_TO_HEDGE", **base)
    if book is None:
        return _no(cond, primary_token_id, hedge_token, "NO_BOOK", **base)
    if book.token_id != hedge_token or book.condition_id != cond:
        return _no(cond, primary_token_id, hedge_token, "BOOK_TOKEN_MISMATCH", **base)
    base["book_received_at"] = book.received_at
    base["best_ask"] = book.best_ask
    if book.is_stale(now, max_book_age):
        return _no(cond, primary_token_id, hedge_token, "STALE_BOOK", **base)
    if not book.asks:
        return _no(cond, primary_token_id, hedge_token, "NO_ASK_LIQUIDITY", **base)

    # Two-pointer walk over cheapest lots and best asks, stopping at the first segment
    # whose marginal net profit per matched share falls below the configured gap.
    ordered = sorted(lots, key=lambda lt: (lt.cost_per_share, lt.lot_id))
    li, lot_left = 0, ordered[0].unhedged_shares
    matched = ZERO
    price_cap: Decimal | None = None
    cap_qty = min(total_unhedged, settings.max_hedge_shares)
    for level in book.asks:
        if level.price > settings.max_hedge_price:
            break
        net_per = fees.buy_net_per_gross_share(level.price)
        if net_per <= ZERO:
            continue
        cost_per_net = fees.buy_cash_per_gross_share(level.price) / net_per
        level_left = level.size * net_per
        while level_left > ZERO and li < len(ordered) and matched < cap_qty:
            lot = ordered[li]
            marginal = ONE - lot.cost_per_share - cost_per_net - settings.other_cost_per_share
            if marginal < settings.min_net_gap_per_share:
                break
            take = min(level_left, lot_left, cap_qty - matched)
            matched += take
            level_left -= take
            lot_left -= take
            price_cap = level.price
            if lot_left <= ZERO:
                li += 1
                if li < len(ordered):
                    lot_left = ordered[li].unhedged_shares
        else:
            continue
        break

    depth = book.ask_depth_usd(settings.max_hedge_price)
    base["depth_usd"] = depth
    if matched <= ZERO or price_cap is None:
        return _no(cond, primary_token_id, hedge_token, "NO_PROFITABLE_DEPTH", **base)

    fill = simulate_buy(
        book, fees, target_net_shares=matched, limit_price=price_cap, share_step=share_step
    )
    if not fill.filled:
        return _no(cond, primary_token_id, hedge_token, "BELOW_SHARE_STEP", **base)

    delivered = fill.net_shares
    allocations: list[LotAllocation] = []
    left = delivered
    for lot in ordered:
        if left <= ZERO:
            break
        take = min(lot.unhedged_shares, left)
        allocations.append(
            LotAllocation(lot.lot_id, take, take * lot.cost_per_share, take * lot.fee_per_share)
        )
        left -= take
    primary_cost = sum((a.primary_cost for a in allocations), ZERO)
    primary_fees = sum((a.primary_fee for a in allocations), ZERO)
    other = delivered * settings.other_cost_per_share
    payout = delivered * ONE
    net = payout - primary_cost - fill.cash_cost - other
    gross = net + primary_fees + fill.fee_usdc + other

    quote = HedgeQuote(
        condition_id=cond,
        primary_token_id=primary_token_id,
        hedge_token_id=hedge_token,
        viable=True,
        reasons=(),
        matched_shares=delivered,
        total_unhedged=total_unhedged,
        allocations=tuple(allocations),
        fill=fill,
        primary_cost=primary_cost,
        primary_fees=primary_fees,
        hedge_cost=fill.cash_cost,
        hedge_fees=fill.fee_usdc,
        other_costs=other,
        payout=payout,
        net_profit=net,
        gross_profit=gross,
        depth_usd=depth,
        best_ask=book.best_ask,
        quoted_at=now,
        book_received_at=book.received_at,
    )
    reasons: list[str] = []
    if delivered < settings.min_hedge_shares:
        reasons.append("BELOW_MIN_HEDGE_SHARES")
    if settings.full_hedge_only and total_unhedged - delivered >= share_step:
        reasons.append("FULL_HEDGE_UNAVAILABLE")
    if quote.net_per_share < settings.min_net_gap_per_share:
        reasons.append("BELOW_MIN_NET_GAP")
    if net < settings.min_abs_profit_usd:
        reasons.append("BELOW_MIN_ABS_PROFIT")
    if quote.return_on_capital < settings.min_return_pct:
        reasons.append("BELOW_MIN_RETURN")
    if depth < settings.min_liquidity_usd:
        reasons.append("INSUFFICIENT_LIQUIDITY")
    if reasons:
        return HedgeQuote(**{**_fields(quote), "viable": False, "reasons": tuple(reasons)})
    return quote


def _fields(q: HedgeQuote) -> dict[str, object]:
    return {name: getattr(q, name) for name in q.__dataclass_fields__}
