from dataclasses import replace

import pytest
from app.db.models import SignalStatus
from app.domain.equity import EquityCoverage, EquityEstimate, unavailable
from app.domain.settings import (
    CopySettings,
    HedgedSellPolicy,
    PaperSettings,
    RiskSettings,
    SellCopyPolicy,
    SizingSettings,
    StrategySettings,
)
from app.risk.pretrade import check_sell, halt_reason
from app.strategy.copy import (
    PAIR_UNWIND_REVIEW,
    BuyContext,
    BuyIntent,
    CopyLot,
    SellContext,
    SellIntent,
    Skip,
    SourceTrade,
    decide_buy,
    decide_sell,
    screen,
)
from app.strategy.sizing import size_copy

from factories import COND, NO, NOW, YES, D, book, market


def trade(**kw) -> SourceTrade:
    base = dict(
        side="BUY",
        condition_id=COND,
        token_id=YES,
        price=D("0.50"),
        shares=D("400"),
        cost=D("200"),
        block_ts=NOW,
    )
    base.update(kw)
    return SourceTrade(**base)


def settings(**copy) -> StrategySettings:
    return StrategySettings(copy=CopySettings(**copy))


def src_equity(positions="4000", cash="6000", confidence="0.9") -> EquityEstimate:
    return EquityEstimate(
        wallet="0xabc",
        as_of=NOW,
        observed_at=NOW,
        positions_value=D(positions),
        cash_balance=D(cash),
        coverage=EquityCoverage.POSITIONS_AND_CASH,
        confidence=D(confidence),
    )


def buy_ctx(**kw) -> BuyContext:
    base = dict(
        trade=trade(),
        market=market(),
        equity=D("500"),
        bankroll=D("500"),
        source_equity=src_equity(),
        source_sold_after=False,
        sizing=SizingSettings(),
    )
    base.update(kw)
    return BuyContext(**base)


def codes(result) -> list[str]:
    return [r.code for r in result.reasons]


# --- screen -------------------------------------------------------------------------------


def test_screen_passes_a_valid_followed_buy_and_sell():
    assert screen(trade(), followed=True, settings=settings()) is None
    assert screen(trade(side="SELL"), followed=True, settings=settings()) is None


@pytest.mark.parametrize(
    ("t", "followed", "s", "code"),
    [
        (trade(is_combo=True), True, settings(), "COMBO_UNSUPPORTED"),
        (trade(), False, settings(), "NOT_FOLLOWED"),
        (trade(), True, settings(enabled=False), "COPY_DISABLED"),
        (trade(side="SELL"), True, settings(copy_sells=False), "SELL_COPY_DISABLED"),
        (trade(condition_id=""), True, settings(), "UNMAPPED_EVENT"),
        (trade(token_id=""), True, settings(), "UNMAPPED_EVENT"),
        (trade(price=D("0")), True, settings(), "UNMAPPED_EVENT"),
        (trade(price=D("1")), True, settings(), "UNMAPPED_EVENT"),
        (trade(price=D("1.2")), True, settings(), "UNMAPPED_EVENT"),
    ],
)
def test_screen_reason_codes(t, followed, s, code):
    r = screen(t, followed=followed, settings=s)
    assert r is not None and r.code == code


def test_sells_disabled_does_not_block_buys():
    assert screen(trade(side="BUY"), followed=True, settings=settings(copy_sells=False)) is None


def test_screen_checks_in_priority_order():
    # Every gate fails at once: the earliest one wins, and peeling it off exposes the next.
    bad = dict(is_combo=True, side="SELL", condition_id="", price=D("0"))
    disabled = settings(enabled=False, copy_sells=False)
    order = [
        ("COMBO_UNSUPPORTED", trade(**bad), False, disabled),
        ("NOT_FOLLOWED", trade(**{**bad, "is_combo": False}), False, disabled),
        ("COPY_DISABLED", trade(**{**bad, "is_combo": False}), True, disabled),
        (
            "SELL_COPY_DISABLED",
            trade(**{**bad, "is_combo": False}),
            True,
            settings(copy_sells=False),
        ),
        ("UNMAPPED_EVENT", trade(**{**bad, "is_combo": False}), True, settings()),
    ]
    for code, t, followed, s in order:
        assert screen(t, followed=followed, settings=s).code == code


# --- decide_buy ---------------------------------------------------------------------------


def test_decide_buy_check_order():
    closed_unknown_token = market(closed=True, tokens=(NO, "3333"))
    # SOURCE_SOLD_AFTER beats a missing market and is a SKIP (not a rejection).
    r = decide_buy(buy_ctx(source_sold_after=True, market=None))
    assert isinstance(r, Skip) and r.status is SignalStatus.SKIPPED
    assert codes(r) == ["SOURCE_SOLD_AFTER"]
    # MARKET_UNAVAILABLE before anything market-derived.
    r = decide_buy(buy_ctx(market=None))
    assert isinstance(r, Skip) and r.status is SignalStatus.REJECTED
    assert codes(r) == ["MARKET_UNAVAILABLE"]
    # TOKEN_MISMATCH before MARKET_CLOSED.
    r = decide_buy(buy_ctx(market=closed_unknown_token))
    assert r.status is SignalStatus.REJECTED and codes(r) == ["TOKEN_MISMATCH"]
    r = decide_buy(buy_ctx(market=market(closed=True)))
    assert r.status is SignalStatus.REJECTED and codes(r) == ["MARKET_CLOSED"]


def test_market_not_accepting_orders_is_closed():
    m = market().model_copy(update={"accepting_orders": False})
    r = decide_buy(buy_ctx(market=m))
    assert isinstance(r, Skip) and codes(r) == ["MARKET_CLOSED"]


def test_market_rejections_happen_before_sizing():
    # Sizing would also skip (zero user equity) but the market problem must be reported.
    r = decide_buy(buy_ctx(market=market(closed=True), equity=D("0")))
    assert codes(r) == ["MARKET_CLOSED"] and r.record == {}


def test_buy_intent_budget_equals_sizing_target_and_resolves_token():
    ctx = buy_ctx()
    expected = size_copy(
        source_trade_cost=ctx.trade.cost,
        source_equity=ctx.source_equity,
        user_equity=ctx.equity,
        settings=ctx.sizing,
    )
    r = decide_buy(ctx)
    assert isinstance(r, BuyIntent)
    assert r.budget == expected.target_budget == D("10")
    assert r.token.token_id == YES and r.market.condition_id == COND
    assert r.reasons == ()
    assert r.record == {
        "source_equity": D("10000"),
        "source_equity_coverage": "POSITIONS_AND_CASH",
        "source_confidence": D("0.9"),
        "source_alloc_pct": D("0.02"),
        "user_equity": D("500"),
        "proposed_pct": D("0.02"),
        "target_budget": D("10"),
    }


def test_buy_on_second_outcome_token_resolves_that_token():
    r = decide_buy(buy_ctx(trade=trade(token_id=NO)))
    assert isinstance(r, BuyIntent) and r.token.token_id == NO and r.token.outcome_index == 1


def test_unknown_fees_add_note_but_still_buy():
    r = decide_buy(buy_ctx(market=market(fees_known=False)))
    assert isinstance(r, BuyIntent) and codes(r) == ["FEES_ASSUMED"]


def test_record_uses_equity_floored_at_trade_cost():
    # Source "equity" below its own trade cost: sizing uses the cost; record must match it.
    r = decide_buy(buy_ctx(source_equity=src_equity(positions="50", cash="0")))
    assert isinstance(r, BuyIntent)
    assert r.record["source_equity"] == D("200")
    assert r.record["source_alloc_pct"] == D("1")


def test_sizing_skip_carries_record_and_prior_notes():
    est = unavailable("0xabc", NOW, NOW, "api down")
    r = decide_buy(buy_ctx(source_equity=est, market=market(fees_known=False)))
    assert isinstance(r, Skip) and r.status is SignalStatus.SKIPPED
    assert codes(r) == ["FEES_ASSUMED", "SIZING_NOTE", "SOURCE_EQUITY_UNKNOWN"]
    assert r.record["source_equity"] is None
    assert r.record["source_equity_coverage"] == "UNAVAILABLE"
    assert r.record["source_confidence"] == D("0")
    assert r.record["user_equity"] == D("500")
    assert r.record["target_budget"] == D("0")
    assert r.record["source_alloc_pct"] is None


def test_sizing_notes_become_reasons_on_intent():
    est = src_equity().model_copy(
        update={"cash_balance": None, "coverage": EquityCoverage.POSITIONS_ONLY}
    )
    r = decide_buy(buy_ctx(source_equity=est))
    assert isinstance(r, BuyIntent)
    assert codes(r) == ["SIZING_NOTE"]
    assert r.record["source_equity_coverage"] == "POSITIONS_ONLY"


# --- decide_sell --------------------------------------------------------------------------


def lots(*spec: tuple[int, str, str]) -> tuple[CopyLot, ...]:
    return tuple(CopyLot(i, D(s), D(m)) for i, s, m in spec)


def sell_ctx(lot_spec, *, sell="50", after="50") -> SellContext:
    return SellContext(
        trade=trade(side="SELL", shares=D(sell)),
        lots=lots(*lot_spec),
        source_position_after=None if after is None else D(after),
    )


def test_sell_intent_preserves_lot_order_and_sums_lots():
    # Lots 7 and 3 (oldest first, ids not sorted): 60 total; source sold half -> 30.
    r = decide_sell(sell_ctx([(7, "40", "0"), (3, "20", "0")]), settings())
    assert isinstance(r, SellIntent)
    assert r.lot_ids == (7, 3)
    assert r.shares == D("30")
    assert r.record == {"target_shares": D("30"), "source_alloc_pct": D("0.5")}


def test_sell_reconstructs_source_position_before_from_after():
    # after=0 means the source sold everything it had -> full exit of our lots.
    r = decide_sell(sell_ctx([(1, "40", "0")], sell="100", after="0"), settings())
    assert isinstance(r, SellIntent) and r.shares == D("40")
    assert r.record["source_alloc_pct"] == D("1")


def test_unknown_source_position_exits_unhedged_with_reason():
    r = decide_sell(sell_ctx([(1, "40", "10")], after=None), settings())
    assert isinstance(r, SellIntent) and r.shares == D("30")
    assert "SOURCE_INVENTORY_UNKNOWN_FULL_EXIT" in codes(r)
    assert r.record["source_alloc_pct"] is None


def test_no_lots_skips_with_plan_reason():
    r = decide_sell(sell_ctx([]), settings())
    assert isinstance(r, Skip) and r.status is SignalStatus.SKIPPED
    assert codes(r) == ["NO_COPIED_INVENTORY"]
    assert r.record["target_shares"] == D("0")


def test_nothing_to_sell_fallback_when_plan_gives_no_reason():
    # Proportional target below the share step with no hedges: plan gives BELOW_SHARE_STEP.
    # Make target exactly zero with no reason: all inventory matched under PRESERVE yields
    # MATCHED_PAIRS_PRESERVED; so use a sub-step lot instead where floor is 0 and target > 0.
    r = decide_sell(
        sell_ctx([(1, "0.01", "0")], sell="1", after="99"),
        StrategySettings(paper=PaperSettings(share_step=D("0.01"))),
    )
    assert isinstance(r, Skip) and codes(r) == ["BELOW_SHARE_STEP"]


def test_sell_policy_ignore_skips():
    r = decide_sell(sell_ctx([(1, "40", "0")]), settings(sell_policy=SellCopyPolicy.IGNORE))
    assert isinstance(r, Skip) and codes(r) == ["SELL_POLICY_IGNORE"]


def test_fully_hedged_preserve_skips_without_unwind_review():
    r = decide_sell(sell_ctx([(1, "40", "40")], sell="100", after="0"), settings())
    assert isinstance(r, Skip)
    assert codes(r) == ["CAPPED_TO_UNHEDGED_INVENTORY", "MATCHED_PAIRS_PRESERVED"]
    assert PAIR_UNWIND_REVIEW not in codes(r)


def test_fully_hedged_unwind_if_better_skips_with_pair_unwind_review():
    s = settings(hedged_sell_policy=HedgedSellPolicy.UNWIND_IF_BETTER)
    r = decide_sell(sell_ctx([(1, "40", "40")], sell="100", after="0"), s)
    assert isinstance(r, Skip) and r.status is SignalStatus.SKIPPED
    assert codes(r)[-1] == PAIR_UNWIND_REVIEW
    assert "NOTHING_TO_SELL" not in codes(r)


def test_partial_hedge_unwind_review_rides_on_sell_intent():
    s = settings(hedged_sell_policy=HedgedSellPolicy.UNWIND_IF_BETTER)
    r = decide_sell(sell_ctx([(1, "30", "20"), (2, "10", "0")], sell="100", after="0"), s)
    assert isinstance(r, SellIntent)
    assert r.shares == D("20") and r.lot_ids == (1, 2)
    assert PAIR_UNWIND_REVIEW in codes(r)


def test_nothing_to_sell_reason_when_plan_is_silent(monkeypatch):
    # plan_copy_sell never returns zero shares without a reason today; decide_sell still
    # guarantees a skip always carries at least one reason for the UI.
    import app.strategy.copy as copy_mod
    from app.strategy.sell_policy import SellPlan

    monkeypatch.setattr(copy_mod, "plan_copy_sell", lambda **_: SellPlan(D("0"), None, False))
    r = decide_sell(sell_ctx([(1, "40", "0")]), settings())
    assert isinstance(r, Skip) and codes(r) == ["NOTHING_TO_SELL"]


# --- check_sell / halt_reason -------------------------------------------------------------


def risk_settings(**kw) -> StrategySettings:
    return StrategySettings(risk=RiskSettings(**kw))


def test_check_sell_limit_is_best_bid_less_slippage():
    b = book(bids=[("0.40", "10"), ("0.60", "5"), ("0.55", "100")])
    d = check_sell(b, risk_settings(max_slippage_pct=D("0.05")))
    assert d.rejection is None
    assert d.limit_price == D("0.57")  # best bid is 0.60 regardless of input order


def test_check_sell_without_bids_has_no_limit_and_no_rejection():
    for b in (None, book(asks=[("0.5", "10")]), book(bids=[("0.5", "0")])):
        d = check_sell(b, risk_settings())
        assert d.limit_price is None and d.rejection is None


@pytest.mark.parametrize("flag", ["strategies_paused", "emergency_stop"])
def test_halted_sell_is_rejected_even_with_bids(flag):
    s = StrategySettings(**{flag: True})
    d = check_sell(book(bids=[("0.60", "100")]), s)
    assert d.limit_price is None
    assert d.rejection is not None and d.rejection.code == "TRADING_HALTED"
    assert halt_reason(s).code == "TRADING_HALTED"


def test_halt_reason_none_when_running():
    assert halt_reason(StrategySettings()) is None


def test_trade_dataclass_is_frozen():
    # Strategy inputs are shared across stages; mutation would leak between them.
    t = trade()
    with pytest.raises(AttributeError):
        t.price = D("0.9")  # type: ignore[misc]
    assert replace(t, price=D("0.9")).price == D("0.9") and t.price == D("0.50")
