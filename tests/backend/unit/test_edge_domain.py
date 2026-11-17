"""Severe edge cases for the pure domain: boundaries, degenerate inputs, rounding."""

from datetime import timedelta
from decimal import Decimal

import pytest
from app.domain.equity import EquityCoverage, EquityEstimate
from app.domain.fills import simulate_buy, simulate_sell
from app.domain.hedge import HedgeLot, quote_hedge
from app.domain.hedge_state import (
    HedgeEvent,
    HedgeState,
    InvalidHedgeTransition,
    state_after_fill,
    transition,
)
from app.domain.market import FeeSchedule, MarketInfo, OutcomeToken
from app.domain.settings import (
    HedgedSellPolicy,
    HedgeSettings,
    RiskSettings,
    SellCopyPolicy,
    SizingMode,
    SizingSettings,
    UnknownEquityPolicy,
)
from app.risk.pretrade import PortfolioSnapshot, check_buy
from app.strategy.sell_policy import plan_copy_sell
from app.strategy.sizing import size_copy

from factories import COND, NO, NOW, YES, D, book, fees, market

STEP = D("0.01")
ZERO = Decimal(0)


# ---------------------------------------------------------------------------- sizing


def est(positions, cash, confidence="0.9", coverage=None):
    return EquityEstimate(
        wallet="0xabc",
        as_of=NOW,
        observed_at=NOW,
        positions_value=None if positions is None else D(positions),
        cash_balance=None if cash is None else D(cash),
        coverage=coverage
        or (EquityCoverage.POSITIONS_ONLY if cash is None else EquityCoverage.POSITIONS_AND_CASH),
        confidence=D(confidence),
    )


def size(cost="10", src=None, user="100", **s):
    return size_copy(
        source_trade_cost=D(cost),
        source_equity=src,
        user_equity=D(user),
        settings=SizingSettings(**s),
    )


@pytest.mark.parametrize("user", ["0", "-5"])
def test_sizing_no_user_equity_skips_even_for_fixed_modes(user):
    for mode in SizingMode:
        r = size(src=est("1000", "0"), user=user, mode=mode)
        assert r.skipped and r.skip_code == "NO_USER_EQUITY"
        assert r.target_budget == ZERO
        assert r.proposed_copy_pct is None


def test_sizing_observed_zero_source_equity_never_divides_by_zero():
    r = size(src=est("0", "0"))
    assert r.skipped and r.skip_code == "SOURCE_EQUITY_UNKNOWN" and r.target_budget == ZERO
    fb = size(
        src=est("0", "0"),
        unknown_equity_policy=UnknownEquityPolicy.FALLBACK_FIXED,
        fixed_amount_usd=D("3"),
    )
    assert fb.target_budget == D("3") and fb.source_allocation_pct is None


def test_sizing_unavailable_coverage_ignores_stale_positions_value():
    # Coverage UNAVAILABLE wins over a leftover positions number: unknown is not equity.
    r = size(src=est("5000", "5000", coverage=EquityCoverage.UNAVAILABLE))
    assert r.skip_code == "SOURCE_EQUITY_UNKNOWN" and r.target_budget == ZERO


def test_sizing_confidence_exactly_at_minimum_is_accepted():
    ok = size(src=est("1000", "0", confidence="0.3"), min_source_confidence=D("0.3"))
    assert not ok.skipped and ok.target_budget == D("1")
    low = size(src=est("1000", "0", confidence="0.299999"), min_source_confidence=D("0.3"))
    assert low.skip_code == "SOURCE_EQUITY_LOW_CONFIDENCE"


def test_sizing_lower_bound_smaller_than_trade_caps_allocation_at_100pct():
    r = size(cost="500", src=est("100", None), user="40")
    assert r.source_allocation_pct == 1 and r.source_equity_used == D("500")
    assert r.target_budget == D("40")


def test_sizing_budget_is_rounded_down_to_usdc_quantum():
    r = size(cost="1", src=est("3", "0"), user="100")
    # 100 / 3 = 33.333...; rounding up would overspend the true allocation.
    assert r.target_budget == D("33.333333")
    assert r.target_budget <= D("100") * r.source_allocation_pct


def test_sizing_sub_micro_allocation_rounds_to_zero_not_negative():
    r = size(cost="0.01", src=est("1000000000", "0"), user="10")
    assert r.target_budget == ZERO and not r.skipped
    pct = size(user="0.000001", mode=SizingMode.FIXED_PCT_OF_USER, fixed_pct_of_user=D("0.02"))
    assert pct.target_budget == ZERO


def test_sizing_multiplier_extremes():
    hi = size(cost="200", src=est("1000", "0"), user="100", multiplier=D("10"))
    assert hi.target_budget == D("200")  # 20% alloc * 10x: risk caps, sizing does not
    lo = size(cost="200", src=est("1000", "0"), user="100", multiplier=D("0.00000001"))
    assert lo.target_budget == ZERO


def test_sizing_rejects_non_positive_trade_cost():
    with pytest.raises(ValueError):
        size(cost="0", src=est("1000", "0"))


# ------------------------------------------------------------------------------ risk


def snap(**kw):
    base = dict(
        equity=D("1000"),
        cash=D("1000"),
        reserved=D("0"),
        exposure_total=D("0"),
        exposure_by_condition={},
        exposure_by_token={},
        day_pnl=D("0"),
    )
    base.update(kw)
    return PortfolioSnapshot(**base)


def risk(
    budget="20",
    *,
    portfolio=None,
    b=None,
    source_price="0.50",
    delay=timedelta(seconds=5),
    buys_in_market=0,
    **settings,
):
    return check_buy(
        budget=D(budget),
        token_id=YES,
        condition_id=COND,
        source_price=D(source_price),
        source_ts=NOW - delay,
        now=NOW,
        book=b if b is not None else book(asks=[("0.50", "1000")]),
        portfolio=portfolio or snap(),
        settings=RiskSettings(**settings),
        trading_halted=False,
        buys_in_market=buys_in_market,
    )


def test_risk_entry_price_exactly_at_max_is_allowed_one_tick_above_rejected():
    at = risk(b=book(asks=[("0.80", "1000")]), source_price="0.80", max_entry_price=D("0.80"))
    assert not at.rejected and at.limit_price == D("0.80")
    above = risk(b=book(asks=[("0.80", "1000")]), source_price="0.81", max_entry_price=D("0.80"))
    assert above.reject_code == "ENTRY_PRICE_TOO_HIGH"


def test_risk_ask_above_max_entry_rejected_even_when_source_price_is_fine():
    d = risk(b=book(asks=[("0.81", "1000")]), source_price="0.80", max_entry_price=D("0.80"))
    assert d.reject_code == "PRICE_MOVED" and d.approved_budget == ZERO


def test_risk_limit_never_above_hard_max_price_when_max_entry_is_one():
    ok = risk(b=book(asks=[("0.999", "1000")]), source_price="0.999", max_entry_price=D("1"))
    assert not ok.rejected and ok.limit_price == D("0.999")
    # Only asks above 0.999 exist: never buy there, whatever the source paid.
    d = risk(b=book(asks=[("0.9995", "1000")]), source_price="0.9995", max_entry_price=D("1"))
    assert d.reject_code == "PRICE_MOVED"


def test_risk_max_buys_per_market_of_one():
    assert not risk(buys_in_market=0, max_buys_per_market=1).rejected
    assert risk(buys_in_market=1, max_buys_per_market=1).reject_code == "MAX_BUYS_PER_MARKET"


def test_risk_approved_never_exceeds_any_cap_and_is_usdc_floored():
    p = snap(
        equity=D("333.3333337"),
        cash=D("400"),
        reserved=D("10"),
        exposure_by_token={YES: D("1")},
    )
    d = risk("1000", portfolio=p, max_pct_per_trade=D("0.05"), min_cash_reserve_usd=D("0"))
    # 333.3333337 * 0.05 = 16.666666685 -> floored to the micro-dollar
    assert d.approved_budget == D("16.666666")
    assert [r.code for r in d.reasons] == ["MAX_PCT_PER_TRADE"]


def test_risk_tightest_cap_wins_across_all_caps():
    p = snap(
        equity=D("10000"),
        cash=D("100"),
        reserved=D("30"),
        exposure_by_token={YES: D("60")},
        exposure_by_condition={COND: D("140")},
    )
    d = risk("1000", portfolio=p, max_pct_per_trade=D("0.05"), min_order_usd=D("0"))
    caps = {
        "MAX_PCT_PER_TRADE": D("500"),
        "MAX_POSITION": D("40"),
        "MAX_MARKET_EXPOSURE": D("10"),
        "CASH_RESERVE": D("45"),
    }
    assert d.approved_budget == min(caps.values()) == D("10")
    assert d.approved_budget <= p.available_cash - RiskSettings().min_cash_reserve_usd
    assert [r.code for r in d.reasons] == [
        "MAX_PCT_PER_TRADE",
        "MAX_POSITION",
        "MAX_MARKET_EXPOSURE",
    ]


@pytest.mark.parametrize(
    "portfolio",
    [
        snap(exposure_by_token={YES: D("150")}),  # already over max position
        snap(exposure_by_condition={COND: D("999")}),  # over market cap
        snap(exposure_total=D("900")),  # over total exposure pct
        snap(cash=D("10"), reserved=D("40")),  # reservations exceed cash
        snap(equity=D("-50")),  # negative equity
    ],
)
def test_risk_negative_headroom_rejects_and_never_approves_negative(portfolio):
    d = risk("20", portfolio=portfolio, min_order_usd=D("0"))
    assert d.rejected and d.reject_code == "BELOW_MIN_ORDER"
    assert d.approved_budget == ZERO and d.limit_price is None


def test_risk_negative_budget_is_rejected_not_approved():
    d = risk("-5", min_order_usd=D("0"))
    assert d.rejected and d.approved_budget == ZERO


def test_risk_daily_loss_exactly_at_limit_halts():
    assert risk(portfolio=snap(day_pnl=D("-50")), daily_loss_limit_usd=D("50")).reject_code == (
        "DAILY_LOSS_LIMIT"
    )
    assert not risk(portfolio=snap(day_pnl=D("-49.999999")), daily_loss_limit_usd=D("50")).rejected


def test_risk_detection_delay_and_book_age_boundaries():
    assert not risk(delay=timedelta(seconds=300), max_detection_delay_s=300).rejected
    late = risk(delay=timedelta(seconds=300, microseconds=1), max_detection_delay_s=300)
    assert late.reject_code == "STALE_SIGNAL"
    edge = book(asks=[("0.50", "1000")], received_at=NOW - timedelta(seconds=15))
    assert not risk(b=edge, max_book_age_s=15).rejected
    old = book(asks=[("0.50", "1000")], received_at=NOW - timedelta(seconds=15, microseconds=1))
    assert risk(b=old, max_book_age_s=15).reject_code == "STALE_BOOK"


def test_risk_min_liquidity_boundary_counts_only_depth_within_limit():
    exact = book(asks=[("0.50", "50"), ("0.60", "1000")])  # $25 within limit 0.515
    assert not risk(b=exact, min_liquidity_usd=D("25")).rejected
    thin = book(asks=[("0.50", "49.99"), ("0.60", "1000")])
    assert risk(b=thin, min_liquidity_usd=D("25")).reject_code == "INSUFFICIENT_LIQUIDITY"


def test_risk_zero_tolerance_requires_ask_at_source_price():
    tight = {"max_price_diff_pct": D("0"), "max_slippage_pct": D("0")}
    d = risk(b=book(asks=[("0.50", "1000")]), source_price="0.50", **tight)
    assert not d.rejected and d.limit_price == D("0.50")
    assert risk(b=book(asks=[("0.51", "1000")]), **tight).reject_code == "PRICE_MOVED"


# ----------------------------------------------------------------------------- fills


def test_buy_zero_or_negative_budget_spends_nothing():
    b = book(asks=[("0.50", "100")])
    for budget in ("0", "-1"):
        fill = simulate_buy(b, fees(), budget=D(budget))
        assert not fill.filled and fill.cash_delta == ZERO and fill.legs == ()


def test_buy_limit_below_best_ask_spends_nothing():
    fill = simulate_buy(book(asks=[("0.50", "100")]), fees(), budget=D("50"), limit_price=D("0.49"))
    assert not fill.filled and fill.cash_cost == ZERO
    assert fill.reasons == ("BELOW_SHARE_STEP_OR_LIMIT",)


def test_buy_budget_below_one_share_step_spends_nothing():
    # 0.009 / 0.999 = 0.009009 shares < 0.01 step
    fill = simulate_buy(book(asks=[("0.999", "100")]), fees(), budget=D("0.009"))
    assert not fill.filled and fill.cash_cost == ZERO
    coarse = simulate_buy(book(asks=[("0.50", "100")]), fees(), budget=D("0.49"), share_step=D("1"))
    assert not coarse.filled


@pytest.mark.parametrize(
    ("price", "shares", "fee", "cash"),
    [
        ("0.001", "1000", "0.06993", "1.06993"),  # 1000*0.07*0.001*0.999
        ("0.5", "100", "1.75", "51.75"),
        ("0.999", "100", "0.006993", "99.906993"),
    ],
)
def test_buy_cash_fee_mode_at_price_extremes(price, shares, fee, cash):
    b = book(asks=[(price, "100000")])
    fill = simulate_buy(b, fees("0.07", "cash"), target_net_shares=D(shares))
    assert fill.net_shares == fill.gross_shares == D(shares)
    assert fill.fee_usdc == D(fee)
    assert fill.cash_cost == D(cash)


@pytest.mark.parametrize(
    ("price", "net"),
    [
        ("0.001", "930.07"),  # fee shares = rate*(1-p)*gross = 0.07*0.999*1000 = 69.93
        ("0.5", "965"),
        ("0.999", "999.93"),  # 0.07*0.001*1000 = 0.07 shares
    ],
)
def test_buy_share_fee_mode_at_price_extremes(price, net):
    b = book(asks=[(price, "1000")])
    fill = simulate_buy(b, fees("0.07", "shares"), budget=D(price) * 1000)
    assert fill.gross_shares == D("1000")
    assert fill.net_shares == D(net)
    assert fill.cash_cost == D(price) * 1000  # fee taken in shares, not cash
    assert fill.fee_usdc >= ZERO


def test_buy_target_beyond_depth_fills_partially_and_flags_exhaustion():
    b = book(asks=[("0.40", "30"), ("0.45", "20")])
    fill = simulate_buy(b, fees("0.05", "shares"), target_net_shares=D("1000"))
    assert fill.gross_shares == D("50")
    assert fill.net_shares < D("50")
    assert fill.exhausted_book


def test_buy_budget_one_micro_short_of_full_cost_drops_a_step_not_overspends():
    b = book(asks=[("0.50", "100")])
    full = simulate_buy(b, fees("0.05", "cash"), budget=D("51.25"))
    assert full.gross_shares == D("100") and full.cash_cost == D("51.25")
    short = simulate_buy(b, fees("0.05", "cash"), budget=D("51.249999"))
    assert short.gross_shares == D("99.99")
    assert short.cash_cost <= D("51.249999")


def test_buy_both_caps_respects_tighter_one():
    b = book(asks=[("0.50", "1000")])
    fill = simulate_buy(b, fees(), budget=D("10"), target_net_shares=D("100"))
    assert fill.gross_shares == D("20") and fill.cash_cost == D("10")
    fill = simulate_buy(b, fees(), budget=D("100"), target_net_shares=D("5"))
    assert fill.gross_shares == D("5") and fill.cash_cost == D("2.5")


def test_sell_below_share_step_sells_nothing():
    fill = simulate_sell(book(bids=[("0.50", "100")]), fees("0.07"), shares=D("0.009"))
    assert not fill.filled and fill.cash_delta == ZERO


def test_sell_limit_above_best_bid_sells_nothing():
    fill = simulate_sell(
        book(bids=[("0.50", "100")]), fees(), shares=D("10"), limit_price=D("0.51")
    )
    assert not fill.filled and fill.cash_delta == ZERO


def test_sell_level_size_off_step_is_trimmed_to_step():
    fill = simulate_sell(book(bids=[("0.50", "0.015")]), fees(), shares=D("1"))
    assert fill.gross_shares == D("0.01")
    assert fill.cash_delta == D("0.005")


def test_sell_at_min_price_with_fees_keeps_nonnegative_proceeds():
    fill = simulate_sell(book(bids=[("0.001", "10")]), fees("0.07"), shares=D("10"))
    # notional 0.01, fee 10*0.07*0.001*0.999 = 0.00069930 -> 0.000700
    assert fill.fee_usdc == D("0.000700")
    assert fill.cash_delta == D("0.009300")


def test_buy_share_fee_dust_never_charges_cash_for_zero_delivered_shares():
    # gross 0.000001 at 0.50, fee 3.5e-8 shares -> net floors to 0; cash would round up.
    fill = simulate_buy(
        book(asks=[("0.50", "10")]),
        fees("0.07", "shares"),
        target_net_shares=D("0.000001"),
        share_step=D("0.000001"),
    )
    assert not fill.filled and fill.cash_cost == ZERO


def test_sell_dust_proceeds_never_negative_with_fine_share_step():
    # 0.000001 shares at 0.50: notional 0.0000005 floors to 0, fee rounds up to 0.000001.
    fill = simulate_sell(
        book(bids=[("0.50", "1")]), fees("0.07"), shares=D("0.000001"), share_step=D("0.000001")
    )
    assert not fill.filled and fill.cash_delta == ZERO


# ---------------------------------------------------------------------- sell policy


def sell(sell="10", before="100", total="40", matched="0", **kw):
    return plan_copy_sell(
        source_sell_shares=D(sell),
        source_position_before=None if before is None else D(before),
        our_total_shares=D(total),
        our_matched_shares=D(matched),
        policy=kw.get("policy", SellCopyPolicy.PROPORTIONAL),
        hedged_policy=kw.get("hedged", HedgedSellPolicy.PRESERVE_HEDGE),
        share_step=kw.get("step", STEP),
    )


def test_sell_plan_matched_exceeding_total_never_sells():
    for policy in (SellCopyPolicy.PROPORTIONAL, SellCopyPolicy.FULL_EXIT):
        p = sell(sell="100", total="40", matched="45", policy=policy)
        assert p.shares_to_sell == ZERO


def test_sell_plan_unknown_source_position_never_sells_matched():
    p = sell(before=None, total="40", matched="40")
    assert p.shares_to_sell == ZERO
    assert "MATCHED_PAIRS_PRESERVED" in p.reasons


def test_sell_plan_dust_below_step_is_reported_not_sold():
    p = sell(sell="100", before="100", total="0.009")
    assert p.shares_to_sell == ZERO and "BELOW_SHARE_STEP" in p.reasons


def test_sell_plan_fraction_floors_to_step():
    p = sell(sell="1", before="3", total="10")
    assert p.shares_to_sell == D("3.33")
    assert p.shares_to_sell <= D("10") * p.source_sell_fraction


def test_sell_plan_partial_source_sell_with_hedge_caps_to_unhedged():
    p = sell(sell="50", before="100", total="100", matched="90")
    assert p.shares_to_sell == D("10")
    assert "CAPPED_TO_UNHEDGED_INVENTORY" in p.reasons


def test_sell_plan_unwind_only_on_full_exit():
    unwind = HedgedSellPolicy.UNWIND_IF_BETTER
    partial = sell(sell="99.99", before="100", total="40", matched="40", hedged=unwind)
    assert not partial.consider_pair_unwind
    full_exit = sell(
        sell="1",
        before="100",
        total="40",
        matched="40",
        hedged=unwind,
        policy=SellCopyPolicy.FULL_EXIT,
    )
    assert full_exit.consider_pair_unwind
    unknown = sell(before=None, total="40", matched="40", hedged=unwind)
    assert unknown.consider_pair_unwind


def test_sell_plan_rejects_non_positive_source_sell():
    with pytest.raises(ValueError):
        sell(sell="0")


# ----------------------------------------------------------------------------- hedge


def hq(lots, asks, *, fee_rate="0", fee_mode="cash", mkt=None, b=..., age=0, **hs):
    if b is ...:
        b = book(NO, asks=asks, received_at=NOW - timedelta(seconds=age))
    return quote_hedge(
        market=mkt or market(fee_rate=fee_rate),
        primary_token_id=YES,
        lots=lots,
        book=b,
        fees=fees(fee_rate, fee_mode),
        settings=HedgeSettings(**hs),
        now=NOW,
        max_book_age=timedelta(seconds=15),
        share_step=STEP,
    )


def lot(shares="100", cost="0.40", lot_id=1, token=YES):
    return HedgeLot(
        lot_id=lot_id, token_id=token, unhedged_shares=D(shares), cost_per_share=D(cost)
    )


def test_hedge_combined_cost_exactly_one_minus_gap_is_viable():
    q = hq([lot(cost="0.40")], [("0.57", "500")])
    assert q.viable, q.reasons
    assert q.net_profit == D("3") and q.net_per_share == D("0.03")
    assert hq([lot(cost="0.40")], [("0.58", "500")]).reasons == ("NO_PROFITABLE_DEPTH",)


def test_hedge_boundary_includes_cash_fee():
    # hedge all-in = 0.50 + 0.04*0.5*0.5 = 0.51; 1 - 0.46 - 0.51 = 0.03 exactly
    q = hq([lot(cost="0.46")], [("0.50", "500")], fee_rate="0.04")
    assert q.viable, q.reasons
    assert q.hedge_cost == D("51") and q.hedge_fees == D("1") and q.net_profit == D("3")
    # Share-fee mode makes each delivered share dearer (0.5/0.98): same book is not viable.
    assert not hq([lot(cost="0.46")], [("0.50", "500")], fee_rate="0.04", fee_mode="shares").viable


def test_hedge_other_cost_per_share_boundary():
    ok = hq([lot()], [("0.55", "500")], other_cost_per_share=D("0.02"))
    assert ok.viable and ok.other_costs == D("2") and ok.net_profit == D("3")
    no = hq([lot()], [("0.55", "500")], other_cost_per_share=D("0.020001"))
    assert no.reasons == ("NO_PROFITABLE_DEPTH",)


def test_hedge_ask_exactly_at_max_hedge_price_is_usable():
    q = hq([lot(cost="0.001")], [("0.96", "500")], max_hedge_price=D("0.96"))
    assert q.viable and q.matched_shares == D("100")
    assert hq([lot(cost="0.001")], [("0.961", "500")], max_hedge_price=D("0.96")).reasons == (
        "NO_PROFITABLE_DEPTH",
    )


def test_hedge_ignores_zero_and_negative_unhedged_lots():
    lots = [lot("0", lot_id=1), lot("-5", lot_id=2), lot("50", lot_id=3)]
    q = hq(lots, [("0.55", "500")])
    assert q.total_unhedged == D("50")
    assert q.matched_shares == D("50")
    assert [a.lot_id for a in q.allocations] == [3]


def test_hedge_max_shares_below_share_step_is_not_filled():
    q = hq([lot()], [("0.55", "500")], max_hedge_shares=D("0.005"), min_hedge_shares=D("0"))
    assert not q.viable and q.reasons == ("BELOW_SHARE_STEP",) and q.fill is None


def test_hedge_book_age_boundary():
    assert hq([lot()], [("0.55", "500")], age=15).viable
    assert hq([lot()], [("0.55", "500")], age=16).reasons == ("STALE_BOOK",)


def test_hedge_market_gates():
    ok_asks = [("0.55", "500")]
    assert hq([lot()], ok_asks, mkt=market(fees_known=False)).reasons == ("FEES_UNKNOWN",)
    assert hq([lot()], ok_asks, mkt=market(verified=False)).reasons == ("COMPLEMENT_UNVERIFIED",)
    neg = hq([lot()], ok_asks, mkt=market(neg_risk=True), allow_neg_risk=True)
    assert neg.viable
    resolved = market().model_copy(update={"resolved": True})
    assert hq([lot()], ok_asks, mkt=resolved).reasons == ("MARKET_NOT_TRADING",)
    halted = market().model_copy(update={"accepting_orders": False})
    assert hq([lot()], ok_asks, mkt=halted).reasons == ("MARKET_NOT_TRADING",)


def test_hedge_input_mismatches():
    asks = [("0.55", "500")]
    assert hq([lot(token=NO)], asks).reasons == ("LOT_TOKEN_MISMATCH",)
    assert hq([lot()], asks, b=None).reasons == ("NO_BOOK",)
    other_cond = book(NO, asks=asks, condition_id="0xdead")
    assert hq([lot()], asks, b=other_cond).reasons == ("BOOK_TOKEN_MISMATCH",)
    foreign = MarketInfo(
        condition_id=COND,
        tokens=(
            OutcomeToken(token_id="7777", outcome="Yes", outcome_index=0),
            OutcomeToken(token_id=NO, outcome="No", outcome_index=1),
        ),
        complement_verified=True,
        fetched_at=NOW,
    )
    assert hq([lot()], asks, mkt=foreign).reasons == ("NOT_STANDARD_BINARY",)


def test_hedge_share_fee_allocations_cover_exactly_delivered_shares():
    lots = [lot("30", "0.30", lot_id=1), lot("30", "0.35", lot_id=2), lot("30", "0.38", lot_id=3)]
    q = hq(lots, [("0.50", "40"), ("0.55", "1000")], fee_rate="0.02", fee_mode="shares")
    assert q.fill is not None
    alloc_total = sum((a.shares for a in q.allocations), ZERO)
    assert alloc_total == q.matched_shares == q.fill.net_shares
    for a in q.allocations:
        src = next(lt for lt in lots if lt.lot_id == a.lot_id)
        assert ZERO < a.shares <= src.unhedged_shares
    assert q.primary_cost == sum((a.primary_cost for a in q.allocations), ZERO)


# ------------------------------------------------------------------------ hedge state


@pytest.mark.parametrize("event", list(HedgeEvent))
def test_settled_is_terminal_for_every_event(event):
    with pytest.raises(InvalidHedgeTransition):
        transition(HedgeState.SETTLED, event)


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (HedgeState.UNHEDGED, HedgeEvent.FILL_RECONCILED_FULL),
        (HedgeState.UNHEDGED, HedgeEvent.FILL_RECONCILED_PARTIAL),
        (HedgeState.HEDGE_OPPORTUNITY, HedgeEvent.FILL_RECONCILED_PARTIAL),
        (HedgeState.HEDGE_PENDING, HedgeEvent.ORDER_SUBMITTED),
        (HedgeState.FULLY_HEDGED, HedgeEvent.ORDER_SUBMITTED),
        (HedgeState.FULLY_HEDGED, HedgeEvent.ORDER_FAILED),
        (HedgeState.HEDGE_FAILED, HedgeEvent.FILL_RECONCILED_FULL),
    ],
)
def test_illegal_hedge_transitions_raise(state, event):
    with pytest.raises(InvalidHedgeTransition):
        transition(state, event)


def test_fully_hedged_only_entered_via_full_reconcile():
    for state in HedgeState:
        for event in HedgeEvent:
            try:
                new = transition(state, event)
            except InvalidHedgeTransition:
                continue
            if new is HedgeState.FULLY_HEDGED:
                assert event is HedgeEvent.FILL_RECONCILED_FULL, (state, event)


def test_state_after_fill_requires_booked_matched_shares():
    assert state_after_fill(D("0"), D("0")) is HedgeEvent.FILL_RECONCILED_PARTIAL
    assert state_after_fill(D("0"), D("10")) is HedgeEvent.FILL_RECONCILED_PARTIAL
    assert state_after_fill(D("10"), D("0.000001")) is HedgeEvent.FILL_RECONCILED_PARTIAL
    assert state_after_fill(D("10"), D("0")) is HedgeEvent.FILL_RECONCILED_FULL
    assert state_after_fill(D("10"), D("-1")) is HedgeEvent.FILL_RECONCILED_FULL


# ------------------------------------------------------------------------------ fees


def test_fee_is_zero_at_price_bounds_and_symmetric():
    s = FeeSchedule(rate=D("0.07"))
    assert s.fee_usdc(D("100"), D("0")) == ZERO
    assert s.fee_usdc(D("100"), D("1")) == ZERO
    assert s.fee_usdc(D("100"), D("0.2")) == s.fee_usdc(D("100"), D("0.8"))


def test_fee_exponent_above_one():
    s = FeeSchedule(rate=D("0.08"), exponent=2)
    assert s.fee_usdc(D("100"), D("0.5")) == D("0.5")  # 100*0.08*0.25^2
    assert s.fee_usdc(D("100"), D("0")) == ZERO
    assert s.fee_usdc(D("100"), D("0.5")) < FeeSchedule(rate=D("0.08")).fee_usdc(D("100"), D("0.5"))


def test_disabled_fee_schedule_charges_nothing_whatever_the_rate():
    assert FeeSchedule(enabled=False, rate=D("0.5")).fee_usdc(D("100"), D("0.5")) == ZERO


@pytest.mark.parametrize("event", list(HedgeEvent))
def test_scanner_never_reopens_a_settled_position(event):
    from types import SimpleNamespace

    from app.services.hedge_scanner import HedgeScannerService

    pos = SimpleNamespace(state=HedgeState.SETTLED.value)
    assert HedgeScannerService._derive(pos, event) is HedgeState.SETTLED  # type: ignore[arg-type]
