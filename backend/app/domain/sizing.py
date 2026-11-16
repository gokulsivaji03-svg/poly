"""PositionSizingEngine: turns a source BUY into a target copy budget.

    source_allocation_pct = source_trade_cost / source_pre_trade_equity
    copy_budget           = user_pre_trade_equity * source_allocation_pct * multiplier

The budget is a *target*; the RiskEngine caps it and the order-book walk decides what is
actually executable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.money import ONE, ZERO, usdc_down
from app.domain.equity import EquityEstimate
from app.domain.settings import (
    LowerBoundEquityPolicy,
    SizingMode,
    SizingSettings,
    UnknownEquityPolicy,
)


@dataclass(frozen=True, slots=True)
class SizingResult:
    mode: SizingMode
    target_budget: Decimal
    source_allocation_pct: Decimal | None
    proposed_copy_pct: Decimal | None
    """target_budget / user_equity."""
    source_equity_used: Decimal | None
    user_equity_used: Decimal
    skipped: bool = False
    skip_code: str | None = None
    notes: tuple[str, ...] = field(default=())


def _skip(mode: SizingMode, user_equity: Decimal, code: str, note: str) -> SizingResult:
    return SizingResult(mode, ZERO, None, None, None, user_equity, True, code, (note,))


def size_copy(
    *,
    source_trade_cost: Decimal,
    source_equity: EquityEstimate | None,
    user_equity: Decimal,
    settings: SizingSettings,
) -> SizingResult:
    """Compute the target copy budget for a source BUY of ``source_trade_cost`` USDC."""
    if source_trade_cost <= ZERO:
        raise ValueError("source_trade_cost must be positive")
    mode = settings.mode
    if user_equity <= ZERO:
        return _skip(mode, user_equity, "NO_USER_EQUITY", "User equity is zero or negative.")

    notes: list[str] = []
    alloc: Decimal | None = None
    src_eq_used: Decimal | None = None

    match mode:
        case SizingMode.PROPORTIONAL:
            src_eq = source_equity.equity if source_equity else None
            if src_eq is None or src_eq <= ZERO:
                if settings.unknown_equity_policy is UnknownEquityPolicy.FALLBACK_FIXED:
                    budget = settings.fixed_amount_usd
                    notes.append("Source bankroll unavailable; fell back to fixed amount.")
                    return _result(mode, budget, None, None, user_equity, notes)
                return _skip(
                    mode,
                    user_equity,
                    "SOURCE_EQUITY_UNKNOWN",
                    "Source bankroll could not be estimated; proportional sizing skipped.",
                )
            assert source_equity is not None
            if source_equity.confidence < settings.min_source_confidence:
                return _skip(
                    mode,
                    user_equity,
                    "SOURCE_EQUITY_LOW_CONFIDENCE",
                    f"Source equity confidence {source_equity.confidence:.2f} below minimum "
                    f"{settings.min_source_confidence:.2f}.",
                )
            if source_equity.is_lower_bound:
                if settings.lower_bound_equity_policy is LowerBoundEquityPolicy.SKIP:
                    return _skip(
                        mode,
                        user_equity,
                        "SOURCE_EQUITY_LOWER_BOUND",
                        "Source cash balance unobservable; only a lower bound is known.",
                    )
                notes.append(
                    "Source equity is a lower bound (cash unobserved); allocation % is an "
                    "upper estimate and is capped by risk limits."
                )
            # A wallet cannot spend more than it had: pre-trade equity >= trade cost.
            src_eq_used = max(src_eq, source_trade_cost)
            alloc = min(ONE, source_trade_cost / src_eq_used)
            budget = user_equity * alloc * settings.multiplier
        case SizingMode.FIXED_AMOUNT:
            budget = settings.fixed_amount_usd
        case SizingMode.FIXED_PCT_OF_USER:
            budget = user_equity * settings.fixed_pct_of_user
        case SizingMode.SOURCE_MULTIPLE:
            budget = source_trade_cost * settings.source_multiple

    return _result(mode, budget, alloc, src_eq_used, user_equity, notes)


def _result(
    mode: SizingMode,
    budget: Decimal,
    alloc: Decimal | None,
    src_eq: Decimal | None,
    user_equity: Decimal,
    notes: list[str],
) -> SizingResult:
    budget = usdc_down(budget)
    return SizingResult(
        mode=mode,
        target_budget=budget,
        source_allocation_pct=alloc,
        proposed_copy_pct=budget / user_equity,
        source_equity_used=src_eq,
        user_equity_used=user_equity,
        notes=tuple(notes),
    )
