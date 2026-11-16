"""Hedge lifecycle state machine for one (account, condition, primary outcome) position.

FULLY_HEDGED is only reachable through FILL_RECONCILED with zero unmatched shares, i.e.
after opposite shares were actually acquired and booked in the ledger. Open orders and
expected fills never count as locked profit.
"""

from __future__ import annotations

from enum import StrEnum


class HedgeState(StrEnum):
    UNHEDGED = "UNHEDGED"
    HEDGE_OPPORTUNITY = "HEDGE_OPPORTUNITY"
    HEDGE_PENDING = "HEDGE_PENDING"
    PARTIALLY_HEDGED = "PARTIALLY_HEDGED"
    FULLY_HEDGED = "FULLY_HEDGED"
    HEDGE_FAILED = "HEDGE_FAILED"
    SETTLED = "SETTLED"


class HedgeEvent(StrEnum):
    OPPORTUNITY_FOUND = "OPPORTUNITY_FOUND"
    OPPORTUNITY_LOST = "OPPORTUNITY_LOST"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    FILL_RECONCILED_PARTIAL = "FILL_RECONCILED_PARTIAL"
    FILL_RECONCILED_FULL = "FILL_RECONCILED_FULL"
    ORDER_FAILED = "ORDER_FAILED"
    INVENTORY_CHANGED_UNHEDGED = "INVENTORY_CHANGED_UNHEDGED"
    """Primary inventory changed so that unmatched shares exist again (or none at all)."""
    SETTLED = "SETTLED"


class InvalidHedgeTransition(ValueError):
    pass


S, E = HedgeState, HedgeEvent

_TRANSITIONS: dict[tuple[HedgeState, HedgeEvent], HedgeState] = {
    (S.UNHEDGED, E.OPPORTUNITY_FOUND): S.HEDGE_OPPORTUNITY,
    (S.UNHEDGED, E.ORDER_SUBMITTED): S.HEDGE_PENDING,
    (S.HEDGE_OPPORTUNITY, E.OPPORTUNITY_FOUND): S.HEDGE_OPPORTUNITY,
    (S.HEDGE_OPPORTUNITY, E.OPPORTUNITY_LOST): S.UNHEDGED,
    (S.HEDGE_OPPORTUNITY, E.ORDER_SUBMITTED): S.HEDGE_PENDING,
    (S.HEDGE_PENDING, E.FILL_RECONCILED_PARTIAL): S.PARTIALLY_HEDGED,
    (S.HEDGE_PENDING, E.FILL_RECONCILED_FULL): S.FULLY_HEDGED,
    (S.HEDGE_PENDING, E.ORDER_FAILED): S.HEDGE_FAILED,
    (S.PARTIALLY_HEDGED, E.OPPORTUNITY_FOUND): S.PARTIALLY_HEDGED,
    (S.PARTIALLY_HEDGED, E.OPPORTUNITY_LOST): S.PARTIALLY_HEDGED,
    (S.PARTIALLY_HEDGED, E.ORDER_SUBMITTED): S.HEDGE_PENDING,
    (S.PARTIALLY_HEDGED, E.FILL_RECONCILED_FULL): S.FULLY_HEDGED,
    (S.PARTIALLY_HEDGED, E.FILL_RECONCILED_PARTIAL): S.PARTIALLY_HEDGED,
    (S.PARTIALLY_HEDGED, E.INVENTORY_CHANGED_UNHEDGED): S.PARTIALLY_HEDGED,
    (S.FULLY_HEDGED, E.INVENTORY_CHANGED_UNHEDGED): S.PARTIALLY_HEDGED,
    (S.FULLY_HEDGED, E.FILL_RECONCILED_FULL): S.FULLY_HEDGED,
    (S.HEDGE_FAILED, E.OPPORTUNITY_FOUND): S.HEDGE_OPPORTUNITY,
    (S.HEDGE_FAILED, E.OPPORTUNITY_LOST): S.UNHEDGED,
    (S.HEDGE_FAILED, E.ORDER_SUBMITTED): S.HEDGE_PENDING,
    (S.HEDGE_FAILED, E.FILL_RECONCILED_PARTIAL): S.PARTIALLY_HEDGED,
    (S.UNHEDGED, E.INVENTORY_CHANGED_UNHEDGED): S.UNHEDGED,
    (S.HEDGE_OPPORTUNITY, E.INVENTORY_CHANGED_UNHEDGED): S.HEDGE_OPPORTUNITY,
}


def transition(state: HedgeState, event: HedgeEvent) -> HedgeState:
    if state is S.SETTLED:
        raise InvalidHedgeTransition("SETTLED is terminal")
    if event is E.SETTLED:
        return S.SETTLED
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidHedgeTransition(f"{state} --{event}--> (undefined)") from None


def state_after_fill(matched_shares_total: object, unmatched_shares: object) -> HedgeEvent:
    """Pick the reconcile event from booked ledger quantities (Decimals)."""
    from decimal import Decimal

    assert isinstance(matched_shares_total, Decimal) and isinstance(unmatched_shares, Decimal)
    if matched_shares_total > 0 and unmatched_shares <= 0:
        return E.FILL_RECONCILED_FULL
    return E.FILL_RECONCILED_PARTIAL
