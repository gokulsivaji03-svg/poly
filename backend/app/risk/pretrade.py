"""Pre-trade risk: the last gate before cash is reserved and an order reaches a venue.

Pure functions over immutable inputs. ``check_buy`` caps or rejects a proposed budget and
sets the limit price; ``check_sell`` sets the sell limit; ``halt_reason`` is the kill switch
shared by every order producer (copy engine, hedge scanner). Every reduction or rejection
carries a coded reason so the UI can show exactly why an order was smaller or refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.money import ONE, ZERO, usdc_down
from app.domain.market import OrderBook
from app.domain.reasons import Reason
from app.domain.settings import RiskSettings, StrategySettings

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


def halt_reason(settings: StrategySettings) -> Reason | None:
    """Kill switch: paused or emergency-stopped strategies place no orders of any kind."""
    if settings.trading_halted:
        return Reason("TRADING_HALTED", "Strategies are paused or emergency-stopped.")
    return None


@dataclass(frozen=True, slots=True)
class SellDecision:
    limit_price: Decimal | None
    rejection: Reason | None = None


def check_sell(book: OrderBook | None, settings: StrategySettings) -> SellDecision:
    """Sell limit: never below best bid minus the allowed slippage; no limit without a bid."""
    halted = halt_reason(settings)
    if halted is not None:
        return SellDecision(None, halted)
    if book is None or book.best_bid is None:
        return SellDecision(None)
    return SellDecision(book.best_bid * (ONE - settings.risk.max_slippage_pct))


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


def check_buy(
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
    buys_in_market: int,
    wallet_headroom: Decimal | None = None,
) -> RiskDecision:
    """``buys_in_market``: filled copy buys already made in ``condition_id``.
    ``wallet_headroom``: what this source wallet's slice of the bankroll has left, if split."""
    reasons: list[Reason] = []
    if trading_halted:
        return _reject(reasons, "TRADING_HALTED", "Strategies are paused or emergency-stopped.")
    if source_price > settings.max_entry_price:
        return _reject(
            reasons,
            "ENTRY_PRICE_TOO_HIGH",
            f"Source price {source_price} above max entry {settings.max_entry_price}.",
        )
    if settings.max_buys_per_market is not None and buys_in_market >= settings.max_buys_per_market:
        return _reject(
            reasons,
            "MAX_BUYS_PER_MARKET",
            f"Already copied {buys_in_market} buys in this market "
            f"(max {settings.max_buys_per_market}).",
        )

    delay = now - source_ts
    if delay > timedelta(seconds=settings.max_detection_delay_s):
        return _reject(
            reasons,
            "STALE_SIGNAL",
            f"Detected {int(delay.total_seconds())}s after source fill "
            f"(max {settings.max_detection_delay_s}s).",
        )
    if (
        settings.daily_loss_limit_usd is not None
        and -portfolio.day_pnl >= settings.daily_loss_limit_usd
    ):
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

    def cap(limit: Decimal | None, code: str, msg: str) -> None:
        nonlocal approved
        if limit is None:
            return
        limit = max(ZERO, limit)
        if approved > limit:
            reasons.append(Reason(code, f"{msg}: {approved:.2f} -> {limit:.2f}"))
            approved = limit

    r = settings
    cap(
        None if r.max_pct_per_trade is None else portfolio.equity * r.max_pct_per_trade,
        "MAX_PCT_PER_TRADE",
        f"Capped at {r.max_pct_per_trade or 0:.2%} of equity",
    )
    cap(
        None
        if r.max_position_usd is None
        else r.max_position_usd - portfolio.exposure_by_token.get(token_id, ZERO),
        "MAX_POSITION",
        "Capped by max position size",
    )
    cap(
        None
        if r.max_market_exposure_usd is None
        else r.max_market_exposure_usd - portfolio.exposure_by_condition.get(condition_id, ZERO),
        "MAX_MARKET_EXPOSURE",
        "Capped by max market exposure",
    )
    cap(
        None
        if r.max_total_exposure_pct is None
        else portfolio.equity * r.max_total_exposure_pct - portfolio.exposure_total,
        "MAX_TOTAL_EXPOSURE",
        "Capped by max total portfolio exposure",
    )
    cap(wallet_headroom, "WALLET_SLICE", "Capped by this wallet's share of the bankroll")
    cap(
        portfolio.available_cash - settings.min_cash_reserve_usd,
        "CASH_RESERVE",
        "Capped to keep minimum cash reserve",
    )
    approved = usdc_down(approved)

    limit_by_source = source_price * (ONE + settings.max_price_diff_pct)
    limit_by_slip = book.best_ask * (ONE + settings.max_slippage_pct)
    limit_price = min(limit_by_source, limit_by_slip, settings.max_entry_price, MAX_PRICE)
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
    minimum = max(settings.min_order_usd, book.min_order_size * book.best_ask)
    if budget < minimum:
        return _reject(
            reasons, "BELOW_MIN_ORDER", f"Budget {budget:.2f} below minimum order ${minimum:.2f}."
        )
    if approved < minimum or approved <= ZERO:
        return _reject(
            reasons,
            "CAPPED_BELOW_MIN_ORDER",
            f"Limits cut the budget to {approved:.2f}, below minimum order ${minimum:.2f}.",
        )
    return RiskDecision(approved, limit_price, False, tuple(reasons))
