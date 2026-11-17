from decimal import Decimal

from app.domain.equity import EquityCoverage, EquityEstimate, unavailable
from app.domain.settings import (
    LowerBoundEquityPolicy,
    SizingMode,
    SizingSettings,
    UnknownEquityPolicy,
)
from app.strategy.sizing import size_copy
from hypothesis import given, settings
from hypothesis import strategies as st

from factories import NOW, D


def est(positions: str, cash: str | None, confidence: str = "0.9") -> EquityEstimate:
    return EquityEstimate(
        wallet="0xabc",
        as_of=NOW,
        observed_at=NOW,
        positions_value=D(positions),
        cash_balance=None if cash is None else D(cash),
        coverage=EquityCoverage.POSITIONS_ONLY
        if cash is None
        else EquityCoverage.POSITIONS_AND_CASH,
        confidence=D(confidence),
    )


def test_proportional_example_from_spec():
    # Source: $10,000 bankroll, $200 buy = 2%. User: $500 -> $10.
    r = size_copy(
        source_trade_cost=D("200"),
        source_equity=est("4000", "6000"),
        user_equity=D("500"),
        settings=SizingSettings(),
    )
    assert r.source_allocation_pct == D("0.02")
    assert r.target_budget == D("10")
    assert r.proposed_copy_pct == D("0.02")
    assert not r.skipped


def test_multiplier_scales_budget():
    r = size_copy(
        source_trade_cost=D("200"),
        source_equity=est("10000", "0"),
        user_equity=D("500"),
        settings=SizingSettings(multiplier=D("1.5")),
    )
    assert r.target_budget == D("15")


def test_changing_user_equity_changes_budget_proportionally():
    s = SizingSettings()
    small = size_copy(
        source_trade_cost=D("100"), source_equity=est("1000", "0"), user_equity=D("200"), settings=s
    )
    big = size_copy(
        source_trade_cost=D("100"), source_equity=est("1000", "0"), user_equity=D("800"), settings=s
    )
    assert big.target_budget == small.target_budget * 4


def test_unknown_source_equity_skips_by_default():
    r = size_copy(
        source_trade_cost=D("50"),
        source_equity=unavailable("0xabc", NOW, NOW, "api down"),
        user_equity=D("500"),
        settings=SizingSettings(),
    )
    assert r.skipped and r.skip_code == "SOURCE_EQUITY_UNKNOWN"
    assert r.target_budget == 0


def test_unknown_source_equity_can_fall_back_to_fixed():
    r = size_copy(
        source_trade_cost=D("50"),
        source_equity=None,
        user_equity=D("500"),
        settings=SizingSettings(
            unknown_equity_policy=UnknownEquityPolicy.FALLBACK_FIXED, fixed_amount_usd=D("7")
        ),
    )
    assert r.target_budget == D("7")
    assert r.source_allocation_pct is None


def test_lower_bound_equity_is_flagged_or_skipped():
    used = size_copy(
        source_trade_cost=D("100"),
        source_equity=est("1000", None),
        user_equity=D("1000"),
        settings=SizingSettings(),
    )
    assert not used.skipped
    assert any("lower bound" in n for n in used.notes)
    skipped = size_copy(
        source_trade_cost=D("100"),
        source_equity=est("1000", None),
        user_equity=D("1000"),
        settings=SizingSettings(lower_bound_equity_policy=LowerBoundEquityPolicy.SKIP),
    )
    assert skipped.skip_code == "SOURCE_EQUITY_LOWER_BOUND"


def test_low_confidence_is_skipped():
    r = size_copy(
        source_trade_cost=D("10"),
        source_equity=est("100", "0", "0.1"),
        user_equity=D("100"),
        settings=SizingSettings(),
    )
    assert r.skip_code == "SOURCE_EQUITY_LOW_CONFIDENCE"


def test_trade_cost_larger_than_observed_equity_caps_allocation_at_100pct():
    r = size_copy(
        source_trade_cost=D("500"),
        source_equity=est("100", "0"),
        user_equity=D("50"),
        settings=SizingSettings(),
    )
    assert r.source_allocation_pct == 1
    assert r.target_budget == D("50")


def test_alternative_modes():
    base = {"source_trade_cost": D("200"), "source_equity": None, "user_equity": D("400")}
    assert size_copy(
        **base, settings=SizingSettings(mode=SizingMode.FIXED_AMOUNT, fixed_amount_usd=D("12"))
    ).target_budget == D("12")
    assert size_copy(
        **base,
        settings=SizingSettings(mode=SizingMode.FIXED_PCT_OF_USER, fixed_pct_of_user=D("0.05")),
    ).target_budget == D("20")
    assert size_copy(
        **base, settings=SizingSettings(mode=SizingMode.SOURCE_MULTIPLE, source_multiple=D("0.1"))
    ).target_budget == D("20")


@settings(max_examples=200, deadline=None)
@given(
    cost=st.decimals(min_value="0.01", max_value="100000", places=2),
    src=st.decimals(min_value="0.01", max_value="1000000", places=2),
    user=st.decimals(min_value="0.01", max_value="100000", places=2),
)
def test_proportional_budget_never_exceeds_user_equity_times_multiplier(cost, src, user):
    r = size_copy(
        source_trade_cost=cost,
        source_equity=est(str(src), "0"),
        user_equity=user,
        settings=SizingSettings(),
    )
    assert Decimal(0) <= r.target_budget <= user
