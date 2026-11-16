from datetime import timedelta
from decimal import Decimal

from app.domain.hedge import HedgeLot, quote_hedge
from app.domain.settings import HedgeSettings
from hypothesis import given, settings
from hypothesis import strategies as st

from factories import NO, NOW, YES, D, book, fees, market

STEP = D("0.01")


def q(lots, asks, *, fee_rate="0", fee_mode="cash", mkt=None, book_token=NO, received_at=NOW, **hs):
    return quote_hedge(
        market=mkt or market(fee_rate=fee_rate),
        primary_token_id=YES,
        lots=lots,
        book=book(book_token, asks=asks, received_at=received_at),
        fees=fees(fee_rate, fee_mode),
        settings=HedgeSettings(**hs),
        now=NOW,
        max_book_age=timedelta(seconds=15),
        share_step=STEP,
    )


def lot(shares="100", cost="0.40", lot_id=1, fee="0"):
    return HedgeLot(
        lot_id=lot_id,
        token_id=YES,
        unhedged_shares=D(shares),
        cost_per_share=D(cost),
        fee_per_share=D(fee),
    )


def test_spec_example_locks_five_dollars_without_fees():
    quote = q([lot()], [("0.55", "500")])
    assert quote.viable, quote.reasons
    assert quote.matched_shares == D("100")
    assert quote.primary_cost == D("40")
    assert quote.hedge_cost == D("55")
    assert quote.payout == D("100")
    assert quote.net_profit == D("5")
    assert quote.net_per_share == D("0.05")
    assert quote.combined_cost_per_share == D("0.95")
    assert quote.remaining_unmatched == 0


def test_apparently_profitable_hedge_rejected_once_fees_are_included():
    # Top-of-book suggests 1 - 0.40 - 0.55 = 0.05/share > 0.03 gap.
    # With a 7% taker fee on both legs: primary basis 0.4168, hedge 0.55 + 0.017325.
    primary_fee = D("0.07") * D("0.40") * D("0.60")
    quote = q(
        [lot(cost=str(D("0.40") + primary_fee), fee=str(primary_fee))],
        [("0.55", "500")],
        fee_rate="0.07",
    )
    assert not quote.viable
    assert quote.reasons == ("NO_PROFITABLE_DEPTH",)


def test_fee_rejection_also_holds_in_share_fee_mode():
    quote = q([lot(cost="0.4168")], [("0.55", "500")], fee_rate="0.07", fee_mode="shares")
    assert not quote.viable


def test_insufficient_depth_rejects_despite_attractive_top_of_book():
    # Only 10 shares at 0.55; the next level at 0.62 is unprofitable.
    quote = q([lot()], [("0.55", "10"), ("0.62", "1000")], min_abs_profit_usd=D("1"))
    assert not quote.viable
    assert quote.matched_shares == D("10")
    assert "BELOW_MIN_ABS_PROFIT" in quote.reasons


def test_full_hedge_only_requires_full_depth():
    quote = q([lot()], [("0.55", "60"), ("0.62", "1000")], full_hedge_only=True)
    assert not quote.viable
    assert "FULL_HEDGE_UNAVAILABLE" in quote.reasons
    partial = q([lot()], [("0.55", "60"), ("0.62", "1000")])
    assert partial.viable and partial.matched_shares == D("60")
    assert partial.remaining_unmatched == D("40")


def test_walks_multiple_levels_and_stops_at_marginal_gap():
    quote = q([lot()], [("0.50", "30"), ("0.56", "30"), ("0.58", "1000")])
    # 0.50 -> 0.10/share, 0.56 -> 0.04/share, 0.58 -> 0.02/share (excluded)
    assert quote.viable
    assert quote.matched_shares == D("60")
    assert quote.hedge_cost == D("0.50") * 30 + D("0.56") * 30
    assert quote.net_profit == D("60") - D("24") - quote.hedge_cost


def test_cheapest_lots_are_matched_first():
    lots = [lot("50", "0.44", lot_id=2), lot("50", "0.40", lot_id=1)]
    quote = q(lots, [("0.54", "1000")])
    assert quote.viable
    assert [a.lot_id for a in quote.allocations] == [1]
    assert quote.matched_shares == D("50")


def test_wrong_complement_token_is_rejected():
    assert q([lot()], [("0.55", "500")], book_token="9999").reasons == ("BOOK_TOKEN_MISMATCH",)
    assert q([lot()], [("0.55", "500")], book_token=YES).reasons == ("BOOK_TOKEN_MISMATCH",)


def test_unsupported_structures_are_skipped():
    multi = market(tokens=(YES, NO, "3333"))
    assert q([lot()], [("0.55", "500")], mkt=multi).reasons == ("NOT_STANDARD_BINARY",)
    neg = market(neg_risk=True)
    assert q([lot()], [("0.55", "500")], mkt=neg).reasons == ("NEG_RISK_UNSUPPORTED",)
    assert q([lot()], [("0.55", "500")], mkt=market(closed=True)).reasons == ("MARKET_NOT_TRADING",)


def test_stale_and_empty_books():
    stale = q([lot()], [("0.55", "500")], received_at=NOW - timedelta(minutes=2))
    assert stale.reasons == ("STALE_BOOK",)
    assert q([lot()], []).reasons == ("NO_ASK_LIQUIDITY",)


def test_already_fully_hedged_lots_are_not_rescanned():
    assert q([lot(shares="0")], [("0.55", "500")]).reasons == ("NOTHING_TO_HEDGE",)


def test_max_hedge_price_and_qty_bounds():
    assert not q([lot()], [("0.55", "500")], max_hedge_price=D("0.50")).viable
    capped = q([lot()], [("0.55", "500")], max_hedge_shares=D("40"))
    assert capped.matched_shares == D("40")


@settings(max_examples=300, deadline=None)
@given(
    shares=st.decimals(min_value="1", max_value="1000", places=2),
    cost=st.decimals(min_value="0.01", max_value="0.95", places=3),
    asks=st.lists(
        st.tuples(
            st.decimals(min_value="0.01", max_value="0.99", places=2),
            st.decimals(min_value="0.01", max_value="1000", places=2),
        ),
        min_size=1,
        max_size=6,
    ),
    rate=st.sampled_from(["0", "0.04", "0.07"]),
    mode=st.sampled_from(["cash", "shares"]),
)
def test_quote_invariants(shares, cost, asks, rate, mode):
    quote = q(
        [lot(str(shares), str(cost))],
        [(str(p), str(s)) for p, s in asks],
        fee_rate=rate,
        fee_mode=mode,
        min_abs_profit_usd=D("0"),
        min_return_pct=D("0"),
        min_liquidity_usd=D("0"),
        min_hedge_shares=D("0"),
    )
    assert quote.matched_shares <= quote.total_unhedged
    if quote.fill is not None:
        assert quote.net_profit == (
            quote.payout - quote.primary_cost - quote.hedge_cost - quote.other_costs
        )
        assert quote.matched_shares == quote.fill.net_shares
    if quote.viable:
        assert quote.net_per_share >= HedgeSettings().min_net_gap_per_share
        assert quote.net_profit > Decimal(0)
