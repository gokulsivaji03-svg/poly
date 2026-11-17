"""Deterministic order-book walk used for sizing, paper execution, and hedge quotes.

Every price, fee and share count in the system flows from these two functions so that the
number shown on a quote is the number booked on a fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from app.core.money import USDC_QUANTUM, ZERO, floor_to_step, shares_down, usdc_up
from app.domain.market import FeeModel, OrderBook, Side


@dataclass(frozen=True, slots=True)
class FillLeg:
    price: Decimal
    gross_shares: Decimal


@dataclass(frozen=True, slots=True)
class FillSimulation:
    side: Side
    legs: tuple[FillLeg, ...]
    gross_shares: Decimal
    """Shares matched against the book."""
    net_shares: Decimal
    """BUY: shares delivered after share-denominated fees. SELL: equals gross."""
    notional: Decimal
    """Sum of price * shares, before fees."""
    fee_usdc: Decimal
    """Fee expressed in USDC (paid in cash or as shares, depending on the fee model)."""
    cash_delta: Decimal
    """Signed change to cash: negative for BUY, positive for SELL."""
    limit_price: Decimal | None
    exhausted_book: bool
    reasons: tuple[str, ...] = field(default=())

    @property
    def filled(self) -> bool:
        return self.gross_shares > ZERO

    @property
    def avg_price(self) -> Decimal | None:
        return self.notional / self.gross_shares if self.gross_shares > ZERO else None

    @property
    def worst_price(self) -> Decimal | None:
        return self.legs[-1].price if self.legs else None

    @property
    def cash_cost(self) -> Decimal:
        """Absolute cash spent (BUY) or received (SELL)."""
        return abs(self.cash_delta)


def _empty(side: Side, limit: Decimal | None, reason: str) -> FillSimulation:
    return FillSimulation(side, (), ZERO, ZERO, ZERO, ZERO, ZERO, limit, False, (reason,))


def _trim_to_total(legs: list[FillLeg], target: Decimal) -> list[FillLeg]:
    """Remove shares from the worst-priced end until the legs sum to ``target``."""
    excess = sum((lg.gross_shares for lg in legs), ZERO) - target
    out = list(legs)
    while excess > ZERO and out:
        last = out[-1]
        if last.gross_shares <= excess:
            excess -= last.gross_shares
            out.pop()
        else:
            out[-1] = FillLeg(last.price, last.gross_shares - excess)
            excess = ZERO
    return out


def _trim_to_step(legs: list[FillLeg], step: Decimal) -> list[FillLeg]:
    total = sum((lg.gross_shares for lg in legs), ZERO)
    return _trim_to_total(legs, floor_to_step(total, step))


def _drop_one_step(legs: list[FillLeg], step: Decimal) -> list[FillLeg]:
    total = sum((lg.gross_shares for lg in legs), ZERO)
    return _trim_to_total(legs, max(ZERO, total - step))


def _buy_totals(legs: list[FillLeg], fees: FeeModel) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return (gross_shares, net_shares, notional, fee_usdc) for BUY legs."""
    gross = sum((lg.gross_shares for lg in legs), ZERO)
    notional = sum((lg.price * lg.gross_shares for lg in legs), ZERO)
    raw_fee = sum((fees.schedule.fee_usdc(lg.gross_shares, lg.price) for lg in legs), ZERO)
    fee = usdc_up(raw_fee)
    if fees.buy_fee_mode == "cash":
        net = gross
    else:
        fee_shares = sum(
            (fees.schedule.fee_usdc(lg.gross_shares, lg.price) / lg.price for lg in legs), ZERO
        )
        net = shares_down(gross - fee_shares)
    return gross, net, notional, fee


def _buy_cash_out(notional: Decimal, fee: Decimal, fees: FeeModel) -> Decimal:
    return usdc_up(notional) + (fee if fees.buy_fee_mode == "cash" else ZERO)


def simulate_buy(
    book: OrderBook,
    fees: FeeModel,
    *,
    budget: Decimal | None = None,
    target_net_shares: Decimal | None = None,
    limit_price: Decimal | None = None,
    share_step: Decimal = Decimal("0.01"),
) -> FillSimulation:
    """Walk asks until the cash ``budget`` or ``target_net_shares`` is reached.

    ``budget`` caps total cash out (notional plus cash fees). ``target_net_shares`` caps
    delivered shares after fees. ``limit_price`` excludes worse levels entirely.
    """
    if budget is None and target_net_shares is None:
        raise ValueError("budget or target_net_shares is required")
    if not book.asks:
        return _empty(Side.BUY, limit_price, "NO_ASK_LIQUIDITY")

    budget_left = budget
    need_left = target_net_shares
    legs: list[FillLeg] = []
    for level in book.asks:
        if limit_price is not None and level.price > limit_price:
            break
        if (budget_left is not None and budget_left <= ZERO) or (
            need_left is not None and need_left <= ZERO
        ):
            break
        net_per = fees.buy_net_per_gross_share(level.price)
        if net_per <= ZERO:
            continue
        cash_per = fees.buy_cash_per_gross_share(level.price)
        take = level.size
        if budget_left is not None:
            take = min(take, budget_left / cash_per)
        if need_left is not None:
            take = min(take, need_left / net_per)
        take = shares_down(take)
        if take <= ZERO:
            break
        legs.append(FillLeg(level.price, take))
        if budget_left is not None:
            budget_left -= take * cash_per
        if need_left is not None:
            need_left -= take * net_per

    legs = _trim_to_step(legs, share_step)
    while True:
        if not legs:
            return _empty(Side.BUY, limit_price, "BELOW_SHARE_STEP_OR_LIMIT")
        gross, net, notional, fee = _buy_totals(legs, fees)
        if net <= ZERO:
            # Share fees consumed every delivered share: never pay for nothing.
            return _empty(Side.BUY, limit_price, "BELOW_SHARE_STEP_OR_LIMIT")
        cash_out = _buy_cash_out(notional, fee, fees)
        over_budget = budget is not None and cash_out > budget
        over_target = target_net_shares is not None and net > target_net_shares
        if not (over_budget or over_target):
            break
        # Quantisation pushed us past a cap; give back one share increment.
        legs = _drop_one_step(legs, share_step)

    consumed_all = len(legs) == len(book.asks) and legs[-1].gross_shares == book.asks[-1].size
    return FillSimulation(
        side=Side.BUY,
        legs=tuple(legs),
        gross_shares=gross,
        net_shares=net,
        notional=notional,
        fee_usdc=fee,
        cash_delta=-cash_out,
        limit_price=limit_price,
        exhausted_book=consumed_all,
    )


def simulate_sell(
    book: OrderBook,
    fees: FeeModel,
    *,
    shares: Decimal,
    limit_price: Decimal | None = None,
    share_step: Decimal = Decimal("0.01"),
) -> FillSimulation:
    """Walk bids selling up to ``shares``; fees reduce cash proceeds."""
    if shares <= ZERO:
        raise ValueError("shares must be positive")
    if not book.bids:
        return _empty(Side.SELL, limit_price, "NO_BID_LIQUIDITY")
    left = floor_to_step(shares, share_step)
    legs: list[FillLeg] = []
    for level in book.bids:
        if left <= ZERO or (limit_price is not None and level.price < limit_price):
            break
        take = min(level.size, left)
        legs.append(FillLeg(level.price, take))
        left -= take
    legs = _trim_to_step(legs, share_step)
    if not legs:
        return _empty(Side.SELL, limit_price, "BELOW_SHARE_STEP_OR_LIMIT")
    gross = sum((lg.gross_shares for lg in legs), ZERO)
    notional = sum((lg.price * lg.gross_shares for lg in legs), ZERO)
    fee = usdc_up(sum((fees.schedule.fee_usdc(lg.gross_shares, lg.price) for lg in legs), ZERO))
    proceeds = notional.quantize(USDC_QUANTUM, rounding=ROUND_DOWN) - fee
    if proceeds <= ZERO:
        # Rounded fee meets or exceeds rounded proceeds: selling would cost cash.
        return _empty(Side.SELL, limit_price, "PROCEEDS_BELOW_FEE")
    consumed_all = len(legs) == len(book.bids) and legs[-1].gross_shares == book.bids[-1].size
    return FillSimulation(
        side=Side.SELL,
        legs=tuple(legs),
        gross_shares=gross,
        net_shares=gross,
        notional=notional,
        fee_usdc=fee,
        cash_delta=proceeds,
        limit_price=limit_price,
        exhausted_book=consumed_all,
    )
