"""How a source SELL translates into a reduction of our copied inventory.

Inventory is always our *actual* ledger holdings, never an assumed mirror of the source.
Matched (hedged) shares are protected: a source exit never silently unwinds a locked pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.money import ONE, ZERO, floor_to_step
from app.domain.settings import HedgedSellPolicy, SellCopyPolicy


@dataclass(frozen=True, slots=True)
class SellPlan:
    shares_to_sell: Decimal
    """Unhedged shares to sell now (never exceeds unhedged inventory)."""
    source_sell_fraction: Decimal | None
    consider_pair_unwind: bool
    """Caller should evaluate selling matched pairs (only under UNWIND_IF_BETTER)."""
    reasons: tuple[str, ...] = field(default=())


def plan_copy_sell(
    *,
    source_sell_shares: Decimal,
    source_position_before: Decimal | None,
    our_total_shares: Decimal,
    our_matched_shares: Decimal,
    policy: SellCopyPolicy,
    hedged_policy: HedgedSellPolicy,
    share_step: Decimal,
) -> SellPlan:
    if source_sell_shares <= ZERO:
        raise ValueError("source_sell_shares must be positive")
    unhedged = max(ZERO, our_total_shares - our_matched_shares)
    reasons: list[str] = []
    fraction: Decimal | None = None
    if source_position_before is not None and source_position_before > ZERO:
        fraction = min(ONE, source_sell_shares / source_position_before)

    if our_total_shares <= ZERO:
        return SellPlan(ZERO, fraction, False, ("NO_COPIED_INVENTORY",))
    if policy is SellCopyPolicy.IGNORE:
        return SellPlan(ZERO, fraction, False, ("SELL_POLICY_IGNORE",))

    if policy is SellCopyPolicy.FULL_EXIT:
        target = unhedged
    elif fraction is None:
        reasons.append("SOURCE_INVENTORY_UNKNOWN_FULL_EXIT")
        target = unhedged
    else:
        target = our_total_shares * fraction
        if target > unhedged:
            reasons.append("CAPPED_TO_UNHEDGED_INVENTORY")
            target = unhedged

    if our_matched_shares > ZERO:
        reasons.append("MATCHED_PAIRS_PRESERVED")
    shares = floor_to_step(min(target, unhedged), share_step)
    if shares <= ZERO and unhedged > ZERO and target > ZERO:
        reasons.append("BELOW_SHARE_STEP")
    consider_unwind = (
        hedged_policy is HedgedSellPolicy.UNWIND_IF_BETTER
        and our_matched_shares > ZERO
        and (fraction is None or fraction >= ONE or policy is SellCopyPolicy.FULL_EXIT)
    )
    return SellPlan(shares, fraction, consider_unwind, tuple(reasons))
