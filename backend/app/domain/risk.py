"""RiskEngine: caps or rejects a proposed copy budget before any cash is reserved.

Every reduction or rejection is returned as a coded, human-readable reason so the UI can
show exactly why a copy was smaller than the source allocation implied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.money import ONE, ZERO, usdc_down
from app.domain.market import OrderBook
from app.domain.settings import RiskSettings

MAX_PRICE = Decimal("0.999")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    equity: Decimal
    cash: Decimal
    reserved: Decimal
    exposure_total: Decimal
    """Marked value of open positions plus outstanding reservations."""
    exposure_by_condition: dict[str, Decimal]
    exposure_by_token: dict[str, Decimal]
    day_pnl: Decimal
    """Equity change since the start of the UTC day (negative = loss)."""

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved_budget: Decimal
    limit_price: Decimal | None
    rejected: bool
    reasons: tuple[Reason, ...] = field(default=())

    @property
    def reject_code(self) -> str | None:
        return self.reasons[-1].code if self.rejected and self.reasons else None


def _reject(reasons: list[Reason], code: str, msg: str) -> RiskDecision:
    return RiskDecision(ZERO, None, True, (*reasons, Reason(code, msg)))


def evaluate_buy(
    *,
    budget: Decimal,
    token_id: str,
    condition_id: str,
    source_price: Decimal,
    source_ts: datetime,
    now: datetime,
    book: OrderBook | None,
    portfolio: PortfolioSnapshot,
    settings: RiskSettings,
    trading_halted: bool,
) -> RiskDecision:
    reasons: list[Reason] = []
    if trading_halted:
        return _reject(reasons, "TRADING_HALTED", "Strategies are paused or emergency-stopped.")

    delay = now - source_ts
    if delay > timedelta(seconds=settings.max_detection_delay_s):
        return _reject(
            reasons,
            "STALE_SIGNAL",
            f"Detected {int(delay.total_seconds())}s after source fill "
            f"(max {settings.max_detection_delay_s}s).",
        )
    if -portfolio.day_pnl >= settings.daily_loss_limit_usd:
        return _reject(
            reasons,
            "DAILY_LOSS_LIMIT",
            f"Day P&L {portfolio.day_pnl:.2f} breaches limit -{settings.daily_loss_limit_usd}.",
        )
    if book is None:
        return _reject(reasons, "NO_BOOK", "No order book available for the outcome token.")
    if book.is_stale(now, timedelta(seconds=settings.max_book_age_s)):
        return _reject(reasons, "STALE_BOOK", "Order book snapshot is older than allowed.")
    if book.best_ask is None:
        return _reject(reasons, "NO_ASK_LIQUIDITY", "No asks on the book.")

    approved = budget

    def cap(limit: Decimal, code: str, msg: str) -> None:
        nonlocal approved
        limit = max(ZERO, limit)
        if approved > limit:
            reasons.append(Reason(code, f"{msg}: {approved:.2f} -> {limit:.2f}"))
            approved = limit

    cap(
        portfolio.equity * settings.max_pct_per_trade,
        "MAX_PCT_PER_TRADE",
        f"Capped at {settings.max_pct_per_trade:.2%} of equity",
    )
    cap(
        settings.max_position_usd - portfolio.exposure_by_token.get(token_id, ZERO),
        "MAX_POSITION",
        "Capped by max position size",
    )
    cap(
        settings.max_market_exposure_usd - portfolio.exposure_by_condition.get(condition_id, ZERO),
        "MAX_MARKET_EXPOSURE",
        "Capped by max market exposure",
    )
    cap(
        portfolio.equity * settings.max_total_exposure_pct - portfolio.exposure_total,
        "MAX_TOTAL_EXPOSURE",
        "Capped by max total portfolio exposure",
    )
    cap(
        portfolio.available_cash - settings.min_cash_reserve_usd,
        "CASH_RESERVE",
        "Capped to keep minimum cash reserve",
    )
    approved = usdc_down(approved)

    limit_by_source = source_price * (ONE + settings.max_price_diff_pct)
    limit_by_slip = book.best_ask * (ONE + settings.max_slippage_pct)
    limit_price = min(limit_by_source, limit_by_slip, MAX_PRICE)
    if book.best_ask > limit_price:
        return _reject(
            reasons,
            "PRICE_MOVED",
            f"Best ask {book.best_ask} exceeds limit {limit_price:.4f} "
            f"(source {source_price}, max diff {settings.max_price_diff_pct:.1%}).",
        )
    depth = book.ask_depth_usd(limit_price)
    if depth < settings.min_liquidity_usd:
        return _reject(
            reasons,
            "INSUFFICIENT_LIQUIDITY",
            f"Only ${depth:.2f} executable within limit {limit_price:.4f} "
            f"(min ${settings.min_liquidity_usd}).",
        )
    if approved < settings.min_order_usd or approved <= ZERO:
        return _reject(
            reasons,
            "BELOW_MIN_ORDER",
            f"Approved budget {approved:.2f} below minimum order ${settings.min_order_usd}.",
        )
    return RiskDecision(approved, limit_price, False, tuple(reasons))
