"""Edge cases for ``backend/scripts/backtest_copy_hedge.py`` on synthetic data (no network)."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from app.domain.market import FeeSchedule
from hypothesis import given
from hypothesis import strategies as st

from factories import NO, YES, D

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend" / "scripts"))
import backtest_copy_hedge as bt

CLOSE = 100_000


class Hist:
    """Stands in for ``Api``: serves only points inside [start, end] like /prices-history."""

    def __init__(self, series: dict[str, list[tuple[int, str]]]) -> None:
        self.series = {tok: [(t, D(p)) for t, p in pts] for tok, pts in series.items()}

    def history(self, token_id: str, start: int, end: int) -> list[tuple[int, Decimal]]:
        return [(t, p) for t, p in self.series.get(token_id, []) if start <= t <= end]


def ns(**kw: object) -> argparse.Namespace:
    """Every field ``main()`` defines, at its defaults, with overrides."""
    base: dict[str, object] = {
        "wallet": "0xabc",
        "markets": 15,
        "rank": "notional",
        "seed": 7,
        "conditions": None,
        "scale": D("0.01"),
        "min_gap": D("0.02"),
        "spread": D("0.01"),
        "delay": 60,
        "min_hedge_price": D("0.02"),
        "cache": Path("/nonexistent"),
        "json": None,
        "entry": "source",
        "entry_delay": 0,
        "max_price_age": 600,
        "max_price_diff": D("0.05"),
        "max_entry_price": D(1),
        "max_buys_per_market": 0,
        "max_market_usd": D(0),
        "min_order_usd": D(1),
    }
    base.update(kw)
    return argparse.Namespace(**base)


def mkt(*, fee_rate: str = "0", final_yes: str = "1", final_no: str | None = None) -> bt.Market:
    no = final_no if final_no is not None else str(1 - D(final_yes))
    return bt.Market(
        condition_id="0xc0nd",
        question="Will it happen?",
        fees=FeeSchedule(enabled=fee_rate != "0", rate=D(fee_rate)),
        fees_known=True,
        final={YES: D(final_yes), NO: D(no)},
        opposite={YES: NO, NO: YES},
        closed_at=CLOSE,
    )


def trade(ts: int, price: str, size: str, token: str = YES) -> dict[str, object]:
    return {"token_id": token, "timestamp": ts, "price": price, "size": size}


def run(
    m: bt.Market,
    buys: list[dict[str, object]],
    sells: list[dict[str, object]] | None = None,
    series: dict[str, list[tuple[int, str]]] | None = None,
    **kw: object,
) -> tuple[list[bt.LotResult], Counter[str]]:
    kw.setdefault("scale", D(1))
    return bt.run_market(Hist(series or {}), m, buys, sells or [], ns(**kw))


# --- _price_at ---------------------------------------------------------------------------


def _brute_price_at(series, ts, max_age):
    past = [(t, p) for t, p in series if t <= ts]
    if not past or ts - past[-1][0] > max_age:
        return None
    return past[-1][1]


@given(
    times=st.lists(st.integers(0, 2_000), unique=True, max_size=30).map(sorted),
    ts=st.integers(-10, 2_100),
    max_age=st.integers(0, 300),
)
def test_price_at_matches_brute_force_and_never_looks_ahead(times, ts, max_age):
    series = [(t, Decimal(i)) for i, t in enumerate(times)]
    got = bt._price_at(series, ts, max_age)
    assert got == _brute_price_at(series, ts, max_age)
    if got is not None:
        assert series[int(got)][0] <= ts


def test_price_at_point_one_second_in_future_is_invisible():
    series = [(100, D("0.40")), (160, D("0.90"))]
    assert bt._price_at(series, 159, 600) == D("0.40")
    assert bt._price_at(series, 160, 600) == D("0.90")
    assert bt._price_at(series, 99, 600) is None


def test_price_at_staleness_boundary_is_inclusive_then_none():
    series = [(1_000, D("0.55"))]
    assert bt._price_at(series, 1_600, 600) == D("0.55")
    assert bt._price_at(series, 1_601, 600) is None
    assert bt._price_at(series, 1_000, 0) == D("0.55")
    assert bt._price_at(series, 1_001, 0) is None


def test_price_at_empty_series_is_none():
    assert bt._price_at([], 10**10, 10**10) is None


# --- run_market: exact P&L -----------------------------------------------------------------


def test_hand_computed_hold_vs_hedge_with_fees_and_sell():
    # 100 YES @0.40, fee 0.1*p*(1-p) -> entry fee 2.4, cost 42.4.
    # NO @0.10 at t=1030 is inside the 60s delay and must be ignored; NO @0.20 at t=1060 is
    # the first eligible minute: ask 0.21, fee 0.1*0.21*0.79 = 0.01659,
    # net 1 - 0.424 - 0.21 - 0.01659 = 0.34941 >= 0.30.
    # Source sells half at 0.70 after the hedge: hold sells 50 (proceeds 35 - 1.05 fee);
    # the hedged lot is untouched by the sell.
    m = mkt(fee_rate="0.1", final_yes="0")
    lots, skips = run(
        m,
        [trade(1_000, "0.40", "100")],
        [trade(2_000, "0.70", "50")],
        {NO: [(1_030, "0.10"), (1_060, "0.20"), (1_120, "0.05")]},
        min_gap=D("0.30"),
    )
    assert skips == Counter()
    [lot] = lots
    assert lot.cost == D("42.4")
    assert (lot.hedge_ts, lot.hedge_price, lot.hedge_fee_rate) == (1_060, D("0.21"), D("0.01659"))
    assert (lot.hold_sold, lot.hold_proceeds) == (D(50), D("33.95"))
    assert lot.baseline_pnl == D("-8.45")
    assert (lot.strat_sold, lot.strat_proceeds) == (D(0), D(0))
    assert lot.strategy_pnl == D("34.941")


def test_fifty_fifty_resolution_settles_both_sides_at_half():
    m = mkt(final_yes="0.5", final_no="0.5")
    # NO at 0.40 -> ask 0.41; net 1 - 0.40 - 0.41 = 0.19 >= 0.10 -> hedged.
    lots, _ = run(
        m,
        [trade(1_000, "0.40", "10"), trade(1_000, "0.40", "10", token=NO)],
        series={NO: [(1_100, "0.40")], YES: [(5_000, "0.98")]},
        min_gap=D("0.10"),
    )
    yes_lot, no_lot = lots
    assert yes_lot.hedge_price == D("0.41")
    assert yes_lot.baseline_pnl == D("1.0")  # 10 * 0.5 - 4
    # Pair pays 0.5 + 0.5 = 1 at a 50/50 resolution: 10 - 4.1 - 4.
    assert yes_lot.strategy_pnl == D("1.9")
    # NO lot never hedged (YES only ever at 0.98 + spread): hold == strategy == 10*0.5 - 4.
    assert no_lot.hedge_price is None
    assert no_lot.baseline_pnl == no_lot.strategy_pnl == D("1.0")


def test_no_hedge_means_strategy_equals_hold_even_with_sells():
    m = mkt(fee_rate="0.05", final_yes="1")
    lots, _ = run(
        m,
        [trade(1_000, "0.30", "100"), trade(1_500, "0.35", "40")],
        [trade(2_000, "0.50", "70"), trade(3_000, "0.20", "500")],
        {NO: [(1_100, "0.60")]},
        min_gap=D("0.90"),
    )
    assert all(lt.hedge_price is None for lt in lots)
    for lt in lots:
        assert (lt.strat_sold, lt.strat_proceeds) == (lt.hold_sold, lt.hold_proceeds)
        assert lt.strategy_pnl == lt.baseline_pnl
    assert sum((lt.hold_sold for lt in lots), D(0)) == D(140)


@given(
    shares=st.decimals("0.000001", "10000", places=6),
    entry=st.decimals("0.001", "0.999", places=3),
    fee=st.decimals("0", "5", places=6),
    final=st.sampled_from([D(0), D("0.5"), D(1)]),
    sold_frac=st.decimals("0", "1", places=3),
    price=st.decimals("0.001", "0.999", places=3),
)
def test_lot_identities(shares, entry, fee, final, sold_frac, price):
    sold = shares * sold_frac
    lot = bt.LotResult(
        ts=0, token_id=YES, shares=shares, entry=entry, entry_fee=fee, final=final,
        hold_sold=sold, hold_proceeds=sold * price, strat_sold=sold, strat_proceeds=sold * price,
    )
    assert lot.strategy_pnl == lot.baseline_pnl
    # Hold P&L is bounded by a binary payoff: at most shares*(1 - entry) - fee.
    assert lot.baseline_pnl <= shares * (1 - entry) - fee
    hedged = bt.LotResult(
        ts=0, token_id=YES, shares=shares, entry=entry, entry_fee=fee, final=final,
        hedge_ts=1, hedge_price=price, hedge_fee_rate=D("0.001"),
    )
    other = bt.LotResult(**{**hedged.__dict__, "final": 1 - final})
    assert hedged.strategy_pnl == other.strategy_pnl  # locked: outcome-independent
    assert hedged.strategy_pnl == shares * (1 - price - D("0.001") - entry) - fee


# --- sells ---------------------------------------------------------------------------------


def test_source_selling_more_than_it_holds_sells_only_our_shares():
    # Cap $20 at 0.50 -> we hold 40 of the source's 100. Source sells 25 (25%) then 150.
    m = mkt(final_yes="0")
    lots, _ = run(
        m,
        [trade(1_000, "0.50", "100")],
        [trade(2_000, "0.60", "25"), trade(3_000, "0.10", "150")],
        max_market_usd=D(20),
        min_gap=D(1),
    )
    [lot] = lots
    assert lot.shares == D(40)
    # 10 @0.60 + the remaining 30 @0.10; never more than held.
    assert lot.hold_sold == D(40)
    assert lot.hold_proceeds == D("9.0")
    assert lot.baseline_pnl == D("-11.0")


def test_sell_before_buy_is_ignored():
    m = mkt(final_yes="1")
    lots, _ = run(m, [trade(1_000, "0.50", "10")], [trade(990, "0.99", "10")], min_gap=D(1))
    [lot] = lots
    assert lot.hold_sold == lot.strat_sold == D(0)
    assert lot.baseline_pnl == D(5)


def test_sell_inside_entry_delay_window_does_not_hit_unfilled_lot():
    # A second buy fills at 1_530 (entry delay 30); a sell at 1_495 shifts to 1_525 and may
    # only touch the first lot.
    m = mkt(final_yes="1")
    lots, _ = run(
        m,
        [trade(1_000, "0.50", "10"), trade(1_500, "0.50", "10")],
        [trade(1_495, "0.80", "10")],
        entry_delay=30,
        min_gap=D(1),
    )
    first, second = lots
    assert first.hold_sold == D(10)
    assert second.hold_sold == D(0)


def test_sells_skip_hedged_lots_and_scale_by_unhedged_holding():
    # Lot A (YES 100 @0.40) hedges at 1_060 via NO @0.20; lot B (YES 100 @0.70 at 1_500)
    # never hedges (no NO price after its fill). Source then sells 100 of its 200 (50%).
    m = mkt(final_yes="0")
    lots, _ = run(
        m,
        [trade(1_000, "0.40", "100"), trade(1_500, "0.70", "100")],
        [trade(2_000, "0.50", "100")],
        {NO: [(1_060, "0.20")]},
        min_gap=D("0.30"),
    )
    a, b = lots
    assert a.hedge_ts == 1_060 and b.hedge_price is None
    # Hold: FIFO 50% of 200 -> all 100 from lot A.
    assert (a.hold_sold, b.hold_sold) == (D(100), D(0))
    # Hedged: only lot B is eligible -> 50% of its 100; lot A stays fully paired.
    assert (a.strat_sold, b.strat_sold) == (D(0), D(50))
    assert a.strategy_pnl == D(100) - D(21) - D(40)
    assert b.strategy_pnl == D(25) - D(70)


def test_market_mode_sell_price_floors_at_0_001():
    m = mkt(final_yes="0")
    lots, _ = run(
        m,
        [trade(1_000, "0.50", "10")],
        [trade(2_000, "0.01", "10")],
        {YES: [(1_000, "0.50"), (2_000, "0.005")]},
        entry="market",
        min_gap=D(1),
    )
    [lot] = lots
    assert lot.hold_sold == D(10)
    assert lot.hold_proceeds == D("0.010")


# --- caps and skips ------------------------------------------------------------------------


def test_max_market_usd_never_exceeded_including_fees():
    m = mkt(fee_rate="0.25", final_yes="1")
    buys = [trade(1_000 + i, p, "40") for i, p in enumerate(["0.31", "0.47", "0.66", "0.83"])]
    lots, skips = run(m, buys, max_market_usd=D(60), min_gap=D(1))
    total = sum((lt.cost for lt in lots), D(0))
    assert total <= D(60)
    assert D(60) - total < D("1e-30")  # last lot is trimmed to the remaining cap
    assert len(lots) == 3 and lots[-1].shares < D(37)
    assert skips == Counter({"MAX_MARKET_EXPOSURE": 1})


def test_max_buys_per_market_counts_only_copied_buys():
    m = mkt()
    buys = [trade(1_000, "0.95", "10")] + [trade(1_001 + i, "0.50", "10") for i in range(4)]
    lots, skips = run(m, buys, max_buys_per_market=2, max_entry_price=D("0.90"), min_gap=D(1))
    assert [lt.ts for lt in lots] == [1_001, 1_002]
    assert skips == Counter({"ENTRY_PRICE_TOO_HIGH": 1, "MAX_BUYS_PER_MARKET": 2})


def test_source_entry_price_boundary():
    m = mkt()
    lots, skips = run(
        m,
        [trade(1_000, "0.90", "10"), trade(1_001, "0.901", "10")],
        max_entry_price=D("0.90"),
        min_gap=D(1),
    )
    assert [lt.entry for lt in lots] == [D("0.90")]
    assert skips == Counter({"ENTRY_PRICE_TOO_HIGH": 1})


@pytest.mark.parametrize(
    ("mid", "copied"),
    [("0.515", True), ("0.5151", False)],
)
def test_market_entry_drift_boundary(mid, copied):
    # Source 0.50, max diff 5% -> ask limit 0.525; ask = mid + 0.01.
    m = mkt()
    lots, skips = run(
        m,
        [trade(1_000, "0.50", "10")],
        series={YES: [(1_005, mid)]},
        entry="market",
        entry_delay=10,
        min_gap=D(1),
    )
    if copied:
        assert [lt.entry for lt in lots] == [D("0.525")]
        assert skips == Counter()
    else:
        assert lots == []
        assert skips == Counter({"PRICE_MOVED": 1})


def test_market_entry_ask_capped_at_0_999_and_by_max_entry_price():
    m = mkt()
    buys = [trade(1_000, "0.99", "10")]
    series = {YES: [(1_000, "0.995")]}
    lots, _ = run(m, buys, series=series, entry="market", min_gap=D(1))
    assert [lt.entry for lt in lots] == [D("0.999")]
    lots, skips = run(
        m, buys, series=series, entry="market", min_gap=D(1), max_entry_price=D("0.99")
    )
    assert lots == [] and skips == Counter({"PRICE_MOVED": 1})


def test_market_entry_uses_price_at_delayed_time_not_future():
    m = mkt()
    lots, _ = run(
        m,
        [trade(1_000, "0.50", "10")],
        series={YES: [(1_000, "0.50"), (1_011, "0.10")]},
        entry="market",
        entry_delay=10,
        min_gap=D(1),
    )
    assert [lt.entry for lt in lots] == [D("0.51")]


def test_market_entry_with_no_history_skips_no_price():
    lots, skips = run(mkt(), [trade(1_000, "0.50", "10")], entry="market", min_gap=D(1))
    assert lots == [] and skips == Counter({"NO_PRICE": 1})


def test_market_entry_sees_minute_price_just_before_first_buy():
    # /prices-history is minute-sampled; the point at 990 is 20s old at our fill (1_010),
    # well within --max-price-age 600.
    lots, skips = run(
        mkt(),
        [trade(1_000, "0.50", "10")],
        series={YES: [(990, "0.50"), (1_050, "0.50")]},
        entry="market",
        entry_delay=10,
        min_gap=D(1),
    )
    assert skips == Counter()
    assert [lt.entry for lt in lots] == [D("0.51")]


def test_min_order_usd_enforced_without_market_cap():
    # $0.50 order with min order $1 is rejected as BELOW_MIN_ORDER, cap or no cap.
    m = mkt()
    buys = [trade(1_000, "0.50", "1")]
    for cap in (D(1_000), D(0)):
        lots, skips = run(m, buys, max_market_usd=cap, min_gap=D(1))
        assert lots == [] and skips == Counter({"BELOW_MIN_ORDER": 1})


# --- hedge timing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "p", "hedged"),
    [
        (1_089, "0.10", False),  # before fill (1_030) + delay 60
        (1_090, "0.10", True),  # exactly fill + delay
        (CLOSE, "0.10", True),  # exactly at close
        (CLOSE + 1, "0.10", False),  # after close
        (1_200, "0.0199", False),  # below min hedge price despite a huge gap
        (1_200, "0.02", True),  # at min hedge price
    ],
)
def test_hedge_window_and_min_price(t, p, hedged):
    m = mkt()
    lots, _ = run(
        m,
        [trade(1_000, "0.40", "10")],
        series={NO: [(t, p)]},
        entry_delay=30,
        min_gap=D("0.30"),
    )
    [lot] = lots
    assert (lot.hedge_ts == t) is hedged
    if not hedged:
        assert lot.hedge_price is None


def test_hedge_triggers_at_exact_min_gap_and_not_below():
    # fee 0.1: entry 0.40 -> cost_ps 0.424. NO 0.20 -> ask 0.21, fee 0.01659 -> net 0.34941.
    m = mkt(fee_rate="0.1")
    buys = [trade(1_000, "0.40", "10")]
    series = {NO: [(1_100, "0.20")]}
    [at] = run(m, buys, series=series, min_gap=D("0.34941"))[0]
    [above] = run(m, buys, series=series, min_gap=D("0.349411"))[0]
    assert at.hedge_price == D("0.21")
    assert above.hedge_price is None
    # Locked P&L per share equals the gap exactly; never above 1 - entry.
    assert at.strategy_pnl == D("3.4941")
    assert at.strategy_pnl / at.shares < 1 - at.entry
