"""User-configurable strategy settings. Persisted as one versioned document."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.market import BuyFeeMode, Dec


class SizingMode(StrEnum):
    PROPORTIONAL = "PROPORTIONAL"
    """Mirror the source's allocation % of its estimated pre-trade equity (default)."""
    FIXED_AMOUNT = "FIXED_AMOUNT"
    """Spend a fixed USD amount per source trade."""
    FIXED_PCT_OF_USER = "FIXED_PCT_OF_USER"
    """Spend a fixed % of the user's active bankroll per source trade."""
    SOURCE_MULTIPLE = "SOURCE_MULTIPLE"
    """Spend the source trade's cost multiplied by a factor."""


class UnknownEquityPolicy(StrEnum):
    SKIP = "SKIP"
    """Skip proportional copies when the source bankroll cannot be estimated at all."""
    FALLBACK_FIXED = "FALLBACK_FIXED"
    """Fall back to ``fixed_amount_usd`` when the source bankroll is unknown."""


class LowerBoundEquityPolicy(StrEnum):
    USE_CAPPED = "USE_CAPPED"
    """Use the lower-bound equity (overstates allocation) and rely on max % caps."""
    SKIP = "SKIP"


class SellCopyPolicy(StrEnum):
    PROPORTIONAL = "PROPORTIONAL"
    """Sell the same fraction of our copied inventory the source sold of theirs."""
    FULL_EXIT = "FULL_EXIT"
    """Any source SELL exits the entire copied (unhedged) inventory in that token."""
    IGNORE = "IGNORE"
    """Never follow source sells; hold until settlement."""


class HedgedSellPolicy(StrEnum):
    PRESERVE_HEDGE = "PRESERVE_HEDGE"
    """Source sells only reduce unhedged shares; matched pairs are held to settlement."""
    UNWIND_IF_BETTER = "UNWIND_IF_BETTER"
    """Sell both legs of matched pairs only if executable proceeds beat locked settlement value."""


class HedgeMode(StrEnum):
    ALERT_ONLY = "ALERT_ONLY"
    AUTO_PAPER = "AUTO_PAPER"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SizingSettings(Model):
    mode: SizingMode = SizingMode.PROPORTIONAL
    multiplier: Dec = Field(default=Decimal("1"), gt=0, le=Decimal("10"))
    fixed_amount_usd: Dec = Field(default=Decimal("10"), gt=0)
    fixed_pct_of_user: Dec = Field(default=Decimal("0.02"), gt=0, le=1)
    source_multiple: Dec = Field(default=Decimal("0.01"), gt=0)
    unknown_equity_policy: UnknownEquityPolicy = UnknownEquityPolicy.SKIP
    lower_bound_equity_policy: LowerBoundEquityPolicy = LowerBoundEquityPolicy.USE_CAPPED
    min_source_confidence: Dec = Field(default=Decimal("0.3"), ge=0, le=1)


class RiskSettings(Model):
    max_pct_per_trade: Dec = Field(default=Decimal("0.05"), gt=0, le=1)
    max_position_usd: Dec = Field(default=Decimal("100"), gt=0)
    max_market_exposure_usd: Dec = Field(default=Decimal("150"), gt=0)
    max_total_exposure_pct: Dec = Field(default=Decimal("0.80"), gt=0, le=1)
    min_liquidity_usd: Dec = Field(default=Decimal("25"), ge=0)
    max_price_diff_pct: Dec = Field(default=Decimal("0.05"), ge=0, le=1)
    """Max worst-fill price above the source's execution price (relative)."""
    max_slippage_pct: Dec = Field(default=Decimal("0.03"), ge=0, le=1)
    """Max worst-fill price above current best ask (relative)."""
    max_detection_delay_s: int = Field(default=300, gt=0)
    min_cash_reserve_usd: Dec = Field(default=Decimal("25"), ge=0)
    daily_loss_limit_usd: Dec = Field(default=Decimal("50"), gt=0)
    max_book_age_s: int = Field(default=15, gt=0)
    min_order_usd: Dec = Field(default=Decimal("1"), ge=0)


class CopySettings(Model):
    enabled: bool = True
    sell_policy: SellCopyPolicy = SellCopyPolicy.PROPORTIONAL
    hedged_sell_policy: HedgedSellPolicy = HedgedSellPolicy.PRESERVE_HEDGE
    copy_sells: bool = True


class HedgeSettings(Model):
    enabled: bool = True
    mode: HedgeMode = HedgeMode.ALERT_ONLY
    min_net_gap_per_share: Dec = Field(default=Decimal("0.03"), ge=0)
    min_abs_profit_usd: Dec = Field(default=Decimal("0.25"), ge=0)
    min_return_pct: Dec = Field(default=Decimal("0.01"), ge=0)
    min_liquidity_usd: Dec = Field(default=Decimal("5"), ge=0)
    max_hedge_price: Dec = Field(default=Decimal("0.97"), gt=0, lt=1)
    min_hedge_shares: Dec = Field(default=Decimal("1"), ge=0)
    max_hedge_shares: Dec = Field(default=Decimal("100000"), gt=0)
    full_hedge_only: bool = False
    other_cost_per_share: Dec = Field(default=Decimal("0"), ge=0)
    """Estimated merge/settlement/operational cost per matched pair."""
    allow_neg_risk: bool = False
    scan_interval_s: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _qty_bounds(self) -> HedgeSettings:
        if self.min_hedge_shares > self.max_hedge_shares:
            raise ValueError("min_hedge_shares must be <= max_hedge_shares")
        return self


class PaperSettings(Model):
    starting_bankroll_usd: Dec = Field(default=Decimal("1000"), gt=0)
    execution_delay_ms: int = Field(default=250, ge=0, le=60_000)
    buy_fee_mode: BuyFeeMode = "shares"
    share_step: Dec = Field(default=Decimal("0.01"), gt=0)
    extra_slippage_bps: int = Field(default=0, ge=0, le=1000)
    """Adverse price adjustment applied to every walked level, in basis points."""


class StrategySettings(Model):
    sizing: SizingSettings = SizingSettings()
    risk: RiskSettings = RiskSettings()
    copy_: CopySettings = Field(default=CopySettings(), alias="copy")
    hedge: HedgeSettings = HedgeSettings()
    paper: PaperSettings = PaperSettings()
    strategies_paused: bool = False
    emergency_stop: bool = False

    model_config = ConfigDict(extra="forbid", validate_assignment=True, populate_by_name=True)

    @property
    def trading_halted(self) -> bool:
        return self.strategies_paused or self.emergency_stop
