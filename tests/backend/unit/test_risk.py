from datetime import timedelta

from app.domain.settings import RiskSettings
from app.risk.pretrade import PortfolioSnapshot, check_buy

from factories import COND, NOW, YES, D, book


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


def run(
    budget="20",
    *,
    portfolio=None,
    b=None,
    source_price="0.50",
    delay_s=5,
    halted=False,
    buys_in_market=0,
    **risk,
):
    return check_buy(
        budget=D(budget),
        token_id=YES,
        condition_id=COND,
        source_price=D(source_price),
        source_ts=NOW - timedelta(seconds=delay_s),
        now=NOW,
        book=b if b is not None else book(asks=[("0.50", "1000")]),
        portfolio=portfolio or snap(),
        settings=RiskSettings(**risk),
        trading_halted=halted,
        buys_in_market=buys_in_market,
    )


def test_rejects_entries_above_max_entry_price():
    d = run(b=book(asks=[("0.95", "1000")]), source_price="0.95", max_entry_price=D("0.90"))
    assert d.reject_code == "ENTRY_PRICE_TOO_HIGH"


def test_limit_price_never_exceeds_max_entry_price():
    d = run(b=book(asks=[("0.88", "1000")]), source_price="0.89", max_entry_price=D("0.90"))
    assert not d.rejected and d.limit_price == D("0.90")


def test_rejects_after_max_buys_per_market():
    assert run(buys_in_market=9, max_buys_per_market=10).rejected is False
    assert run(buys_in_market=10, max_buys_per_market=10).reject_code == "MAX_BUYS_PER_MARKET"


def test_approves_within_limits():
    d = run()
    assert not d.rejected and d.approved_budget == D("20") and d.reasons == ()


def test_caps_at_max_pct_per_trade():
    d = run("200", max_pct_per_trade=D("0.05"))
    assert d.approved_budget == D("50")
    assert d.reasons[0].code == "MAX_PCT_PER_TRADE"


def test_insufficient_cash_caps_to_available_minus_reserve():
    d = run("40", portfolio=snap(cash=D("60"), reserved=D("10")), min_cash_reserve_usd=D("25"))
    assert d.approved_budget == D("25")
    assert "CASH_RESERVE" in {r.code for r in d.reasons}


def test_rejects_when_no_cash_left():
    d = run("40", portfolio=snap(cash=D("30"), reserved=D("10")), min_cash_reserve_usd=D("25"))
    assert d.rejected and d.reject_code == "BELOW_MIN_ORDER"


def test_rejects_stale_signal_and_halt_and_loss_limit():
    assert run(delay_s=10_000).reject_code == "STALE_SIGNAL"
    assert run(halted=True).reject_code == "TRADING_HALTED"
    assert run(portfolio=snap(day_pnl=D("-60"))).reject_code == "DAILY_LOSS_LIMIT"


def test_rejects_price_moved_beyond_source_tolerance():
    d = run(b=book(asks=[("0.60", "1000")]), source_price="0.50", max_price_diff_pct=D("0.05"))
    assert d.reject_code == "PRICE_MOVED"


def test_rejects_thin_liquidity_within_limit():
    d = run(b=book(asks=[("0.50", "10"), ("0.90", "1000")]), min_liquidity_usd=D("25"))
    assert d.reject_code == "INSUFFICIENT_LIQUIDITY"


def test_rejects_stale_and_missing_books():
    assert (
        run(b=book(asks=[("0.5", "100")], received_at=NOW - timedelta(minutes=5))).reject_code
        == "STALE_BOOK"
    )
    assert run(b=book(asks=[])).reject_code == "NO_ASK_LIQUIDITY"


def test_exposure_caps():
    d = run("80", portfolio=snap(exposure_by_token={YES: D("90")}), max_position_usd=D("100"))
    assert d.approved_budget == D("10")
    d = run("80", portfolio=snap(exposure_total=D("790")), max_total_exposure_pct=D("0.8"))
    assert d.approved_budget == D("10")


def test_limit_price_is_min_of_source_and_slippage_bounds():
    d = run(
        b=book(asks=[("0.50", "1000")]),
        source_price="0.50",
        max_price_diff_pct=D("0.10"),
        max_slippage_pct=D("0.02"),
    )
    assert d.limit_price == D("0.51")
