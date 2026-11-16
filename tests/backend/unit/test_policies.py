import pytest
from app.domain.hedge_state import HedgeEvent, HedgeState, InvalidHedgeTransition, transition
from app.domain.sell_policy import plan_copy_sell
from app.domain.settings import HedgedSellPolicy, SellCopyPolicy, StrategySettings

from factories import D

STEP = D("0.01")


def plan(
    sell="50",
    before="100",
    total="40",
    matched="0",
    policy=SellCopyPolicy.PROPORTIONAL,
    hedged=HedgedSellPolicy.PRESERVE_HEDGE,
):
    return plan_copy_sell(
        source_sell_shares=D(sell),
        source_position_before=None if before is None else D(before),
        our_total_shares=D(total),
        our_matched_shares=D(matched),
        policy=policy,
        hedged_policy=hedged,
        share_step=STEP,
    )


def test_proportional_sell_uses_our_actual_inventory():
    p = plan(sell="50", before="100", total="40")
    assert p.shares_to_sell == D("20")
    assert p.source_sell_fraction == D("0.5")


def test_source_sell_larger_than_copied_inventory_never_oversells():
    p = plan(sell="500", before="100", total="40")
    assert p.shares_to_sell == D("40")
    p = plan(sell="5", before="100", total="0")
    assert p.shares_to_sell == 0 and p.reasons == ("NO_COPIED_INVENTORY",)


def test_source_sell_after_full_hedge_preserves_pairs():
    p = plan(sell="100", before="100", total="40", matched="40")
    assert p.shares_to_sell == 0
    assert "MATCHED_PAIRS_PRESERVED" in p.reasons
    assert not p.consider_pair_unwind


def test_partial_hedge_only_sells_unhedged_portion():
    p = plan(sell="100", before="100", total="40", matched="30")
    assert p.shares_to_sell == D("10")


def test_unwind_only_considered_when_explicitly_configured():
    p = plan(
        sell="100", before="100", total="40", matched="40", hedged=HedgedSellPolicy.UNWIND_IF_BETTER
    )
    assert p.consider_pair_unwind


def test_unknown_source_inventory_exits_unhedged_and_says_so():
    p = plan(before=None, total="40", matched="10")
    assert p.shares_to_sell == D("30")
    assert "SOURCE_INVENTORY_UNKNOWN_FULL_EXIT" in p.reasons


def test_ignore_policy():
    assert plan(policy=SellCopyPolicy.IGNORE).shares_to_sell == 0


def test_hedge_state_machine_happy_path():
    s = HedgeState.UNHEDGED
    s = transition(s, HedgeEvent.OPPORTUNITY_FOUND)
    s = transition(s, HedgeEvent.ORDER_SUBMITTED)
    assert s is HedgeState.HEDGE_PENDING
    s = transition(s, HedgeEvent.FILL_RECONCILED_PARTIAL)
    assert s is HedgeState.PARTIALLY_HEDGED
    s = transition(s, HedgeEvent.ORDER_SUBMITTED)
    s = transition(s, HedgeEvent.FILL_RECONCILED_FULL)
    assert s is HedgeState.FULLY_HEDGED
    assert transition(s, HedgeEvent.SETTLED) is HedgeState.SETTLED


def test_fully_hedged_unreachable_without_reconciled_fill():
    with pytest.raises(InvalidHedgeTransition):
        transition(HedgeState.HEDGE_OPPORTUNITY, HedgeEvent.FILL_RECONCILED_FULL)
    with pytest.raises(InvalidHedgeTransition):
        transition(HedgeState.SETTLED, HedgeEvent.OPPORTUNITY_FOUND)


def test_settings_round_trip_with_alias():
    s = StrategySettings.model_validate({"copy": {"sell_policy": "FULL_EXIT"}})
    assert s.copy_.sell_policy is SellCopyPolicy.FULL_EXIT
    dumped = s.model_dump(mode="json", by_alias=True)
    assert dumped["copy"]["sell_policy"] == "FULL_EXIT"
    assert StrategySettings.model_validate(dumped) == s
