"""Market, outcome-token, order-book and fee primitives shared by every strategy."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.core.money import ONE, ZERO, to_decimal

Dec = Annotated[Decimal, BeforeValidator(to_decimal)]


def _opt_dec(v: object) -> Decimal | None:
    return None if v is None else to_decimal(v)


OptDec = Annotated[Decimal | None, BeforeValidator(_opt_dec)]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BookLevel(Frozen):
    price: Dec
    size: Dec

    @model_validator(mode="after")
    def _check(self) -> BookLevel:
        if not (ZERO < self.price < ONE):
            raise ValueError(f"price out of (0,1): {self.price}")
        if self.size < ZERO:
            raise ValueError("negative level size")
        return self


class OrderBook(Frozen):
    """Executable book for one outcome token. Bids sorted desc, asks sorted asc."""

    token_id: str
    condition_id: str
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    tick_size: Dec = Decimal("0.01")
    min_order_size: Dec = ZERO
    exchange_ts: datetime | None = None
    received_at: datetime
    hash: str | None = None

    @model_validator(mode="after")
    def _sorted(self) -> OrderBook:
        bids = tuple(sorted((lv for lv in self.bids if lv.size > ZERO), key=lambda lv: -lv.price))
        asks = tuple(sorted((lv for lv in self.asks if lv.size > ZERO), key=lambda lv: lv.price))
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)
        return self

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def age(self, now: datetime) -> timedelta:
        return now - self.received_at

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        return self.age(now) > max_age

    def ask_depth_usd(self, limit_price: Decimal | None = None) -> Decimal:
        return sum(
            (
                lv.price * lv.size
                for lv in self.asks
                if limit_price is None or lv.price <= limit_price
            ),
            ZERO,
        )

    def bid_depth_usd(self, limit_price: Decimal | None = None) -> Decimal:
        return sum(
            (
                lv.price * lv.size
                for lv in self.bids
                if limit_price is None or lv.price >= limit_price
            ),
            ZERO,
        )

    def mark_price(self) -> tuple[Decimal | None, str]:
        """Conservative mark: midpoint when two-sided, else best bid (liquidation value)."""
        if self.mid is not None:
            return self.mid, "mid"
        if self.best_bid is not None:
            return self.best_bid, "bid"
        return None, "none"


class FeeSchedule(Frozen):
    """Per-market taker fee parameters (Gamma ``feeSchedule``).

    Documented formula: ``fee = C * rate * p * (1 - p)``; ``exponent`` generalises the
    ``p * (1 - p)`` term as published in the market's schedule.
    """

    enabled: bool = True
    rate: Dec = ZERO
    exponent: int = 1
    taker_only: bool = True

    def fee_usdc(self, shares: Decimal, price: Decimal) -> Decimal:
        if not self.enabled or self.rate == ZERO or shares == ZERO:
            return ZERO
        return shares * self.rate * (price * (ONE - price)) ** self.exponent


BuyFeeMode = Literal["shares", "cash"]


class FeeModel(Frozen):
    """How taker fees hit a fill.

    ``shares``: BUY fees reduce the shares delivered (conservative for hedge matching).
    ``cash``: BUY fees are paid in addition to notional. SELL fees always reduce proceeds.
    """

    schedule: FeeSchedule = FeeSchedule(enabled=False)
    buy_fee_mode: BuyFeeMode = "shares"

    def buy_cash_per_gross_share(self, price: Decimal) -> Decimal:
        fee = self.schedule.fee_usdc(ONE, price)
        return price + (fee if self.buy_fee_mode == "cash" else ZERO)

    def buy_net_per_gross_share(self, price: Decimal) -> Decimal:
        if self.buy_fee_mode == "cash":
            return ONE
        return ONE - self.schedule.fee_usdc(ONE, price) / price


class OutcomeToken(Frozen):
    token_id: str
    outcome: str
    outcome_index: int


class MarketInfo(Frozen):
    condition_id: str
    question: str = ""
    slug: str = ""
    event_slug: str = ""
    tokens: tuple[OutcomeToken, ...] = Field(default_factory=tuple)
    neg_risk: bool = False
    fee_schedule: FeeSchedule = FeeSchedule(enabled=False)
    tick_size: Dec = Decimal("0.01")
    min_order_size: Dec = ZERO
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    resolved: bool = False
    payouts: dict[str, Dec] = Field(default_factory=dict)
    """Per-token payout once resolved (1/0, or 0.5/0.5), from on-chain resolution data."""
    fees_known: bool = True
    """False when the fee schedule was unavailable and a conservative rate was assumed."""
    complement_verified: bool = False
    """True only when the token pair was cross-checked against ``/v2/tokens``."""
    end_date: datetime | None = None
    fetched_at: datetime

    @property
    def is_standard_binary(self) -> bool:
        return len(self.tokens) == 2 and len({t.token_id for t in self.tokens}) == 2

    def token(self, token_id: str) -> OutcomeToken | None:
        return next((t for t in self.tokens if t.token_id == token_id), None)

    def complement(self, token_id: str) -> OutcomeToken | None:
        """The other outcome of the same binary condition, or ``None`` if not well-defined."""
        if not self.is_standard_binary or self.token(token_id) is None:
            return None
        return next(t for t in self.tokens if t.token_id != token_id)
