"""Source-wallet equity estimates with explicit coverage and confidence metadata."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.core.money import ZERO
from app.domain.market import Dec, OptDec


class EquityCoverage(StrEnum):
    POSITIONS_AND_CASH = "POSITIONS_AND_CASH"
    """Marked positions and on-chain collateral both observed."""
    POSITIONS_ONLY = "POSITIONS_ONLY"
    """Only marked positions observed: the estimate is a LOWER BOUND on equity."""
    UNAVAILABLE = "UNAVAILABLE"
    """Nothing observable (API failure). Distinct from an observed zero balance."""


class EquityEstimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    wallet: str
    as_of: datetime
    """Point in time the estimate describes (pre-trade for sizing)."""
    observed_at: datetime
    """When the underlying data was fetched."""
    positions_value: OptDec
    cash_balance: OptDec
    coverage: EquityCoverage
    confidence: Dec
    """0..1, decays with snapshot distance from ``as_of`` and missing components."""
    notes: tuple[str, ...] = ()

    @property
    def equity(self) -> Decimal | None:
        if self.coverage is EquityCoverage.UNAVAILABLE or self.positions_value is None:
            return None
        return self.positions_value + (self.cash_balance or ZERO)

    @property
    def is_lower_bound(self) -> bool:
        return self.coverage is EquityCoverage.POSITIONS_ONLY


def unavailable(wallet: str, as_of: datetime, observed_at: datetime, note: str) -> EquityEstimate:
    return EquityEstimate(
        wallet=wallet,
        as_of=as_of,
        observed_at=observed_at,
        positions_value=None,
        cash_balance=None,
        coverage=EquityCoverage.UNAVAILABLE,
        confidence=ZERO,
        notes=(note,),
    )
