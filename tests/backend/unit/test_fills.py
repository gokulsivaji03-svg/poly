from decimal import Decimal

import pytest
from app.domain.fills import simulate_buy, simulate_sell
from hypothesis import given, settings
from hypothesis import strategies as st

from factories import D, book, fees


def test_buy_walks_multiple_levels_without_extrapolating_top_of_book():
    b = book(asks=[("0.40", "100"), ("0.45", "100")])
    fill = simulate_buy(b, fees(), budget=D("60"))
    assert [lg.price for lg in fill.legs] == [D("0.40"), D("0.45")]
    assert fill.gross_shares == D("144.44")
    assert fill.cash_cost == D("59.998")
    assert fill.cash_cost <= D("60")
    assert fill.avg_price is not None and D("0.40") < fill.avg_price < D("0.45")


def test_buy_respects_limit_price():
    b = book(asks=[("0.40", "10"), ("0.50", "1000")])
    fill = simulate_buy(b, fees(), budget=D("100"), limit_price=D("0.45"))
    assert fill.gross_shares == D("10")
    assert fill.worst_price == D("0.40")


def test_buy_cash_fee_mode_charges_documented_formula():
    # fee = C * rate * p * (1-p) = 100 * 0.05 * 0.5 * 0.5 = 1.25
    b = book(asks=[("0.50", "100")])
    fill = simulate_buy(b, fees("0.05", "cash"), target_net_shares=D("100"))
    assert fill.fee_usdc == D("1.25")
    assert fill.cash_cost == D("51.25")
    assert fill.net_shares == D("100")


def test_buy_share_fee_mode_delivers_fewer_shares():
    b = book(asks=[("0.50", "1000")])
    fill = simulate_buy(b, fees("0.05", "shares"), budget=D("50"))
    assert fill.gross_shares == D("100")
    assert fill.cash_cost == D("50")
    # 1.25 USDC fee taken as 2.5 shares at $0.50
    assert fill.net_shares == D("97.5")


def test_target_net_shares_accounts_for_share_fees():
    b = book(asks=[("0.50", "1000")])
    fill = simulate_buy(b, fees("0.05", "shares"), target_net_shares=D("97.5"))
    assert fill.net_shares <= D("97.5")
    assert fill.net_shares >= D("97.48")


def test_empty_book_is_not_fillable():
    fill = simulate_buy(book(asks=[]), fees(), budget=D("10"))
    assert not fill.filled
    assert fill.reasons == ("NO_ASK_LIQUIDITY",)


def test_sell_never_exceeds_requested_and_deducts_fees():
    b = book(bids=[("0.60", "50"), ("0.55", "100")])
    fill = simulate_sell(b, fees("0.04"), shares=D("80"))
    assert fill.gross_shares == D("80")
    assert fill.notional == D("0.60") * 50 + D("0.55") * 30
    assert fill.cash_delta == fill.notional - fill.fee_usdc
    assert fill.fee_usdc > 0


def test_sell_partial_when_book_is_thin():
    fill = simulate_sell(book(bids=[("0.30", "5")]), fees(), shares=D("20"))
    assert fill.gross_shares == D("5")
    assert fill.exhausted_book


levels = st.lists(
    st.tuples(
        st.decimals(min_value="0.01", max_value="0.99", places=2),
        st.decimals(min_value="0.01", max_value="5000", places=2),
    ),
    min_size=0,
    max_size=8,
)


@settings(max_examples=300, deadline=None)
@given(
    asks=levels,
    budget=st.decimals(min_value="0.01", max_value="10000", places=6),
    rate=st.sampled_from(["0", "0.03", "0.07"]),
    mode=st.sampled_from(["cash", "shares"]),
)
def test_buy_never_spends_more_than_budget_or_takes_more_than_book(asks, budget, rate, mode):
    b = book(asks=[(str(p), str(s)) for p, s in asks])
    fill = simulate_buy(b, fees(rate, mode), budget=budget)
    assert fill.cash_cost <= budget
    assert fill.net_shares <= fill.gross_shares
    total_depth = sum((lv.size for lv in b.asks), Decimal(0))
    assert fill.gross_shares <= total_depth
    for leg in fill.legs:
        level_size = sum((lv.size for lv in b.asks if lv.price == leg.price), Decimal(0))
        assert leg.gross_shares <= level_size
    # No funds from nothing: cash out covers at least notional.
    assert fill.cash_cost >= fill.notional - Decimal("0.000001")


@settings(max_examples=300, deadline=None)
@given(
    bids=levels,
    shares=st.decimals(min_value="0.01", max_value="10000", places=2),
    rate=st.sampled_from(["0", "0.05"]),
)
def test_sell_never_sells_more_than_requested(bids, shares, rate):
    b = book(bids=[(str(p), str(s)) for p, s in bids])
    fill = simulate_sell(b, fees(rate), shares=shares)
    assert fill.gross_shares <= shares
    assert fill.cash_delta <= fill.notional
    # Fees can never exceed notional for p in (0,1) and rate < 1: proceeds stay non-negative.
    assert fill.cash_delta >= 0


def test_sell_rejects_non_positive():
    with pytest.raises(ValueError):
        simulate_sell(book(bids=[("0.5", "1")]), fees(), shares=D("0"))
