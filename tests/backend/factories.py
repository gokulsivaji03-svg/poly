"""Deterministic builders for domain objects used across backend tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.market import BookLevel, FeeModel, FeeSchedule, MarketInfo, OrderBook, OutcomeToken

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
COND = "0x" + "ab" * 32
YES = "1111"
NO = "2222"


def D(x: str | int) -> Decimal:
    return Decimal(str(x))


def book(
    token_id: str = YES,
    *,
    asks: list[tuple[str, str]] | None = None,
    bids: list[tuple[str, str]] | None = None,
    condition_id: str = COND,
    received_at: datetime = NOW,
    min_order_size: str = "0",
) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        condition_id=condition_id,
        asks=tuple(BookLevel(price=D(p), size=D(s)) for p, s in (asks or [])),
        bids=tuple(BookLevel(price=D(p), size=D(s)) for p, s in (bids or [])),
        received_at=received_at,
        min_order_size=D(min_order_size),
    )


def market(
    *,
    neg_risk: bool = False,
    fee_rate: str = "0",
    tokens: tuple[str, ...] = (YES, NO),
    closed: bool = False,
) -> MarketInfo:
    return MarketInfo(
        condition_id=COND,
        question="Will it happen?",
        tokens=tuple(
            OutcomeToken(token_id=t, outcome=("Yes", "No", "Maybe")[i], outcome_index=i)
            for i, t in enumerate(tokens)
        ),
        neg_risk=neg_risk,
        fee_schedule=FeeSchedule(enabled=fee_rate != "0", rate=D(fee_rate)),
        closed=closed,
        fetched_at=NOW,
    )


def fees(rate: str = "0", mode: str = "cash") -> FeeModel:
    return FeeModel(
        schedule=FeeSchedule(enabled=rate != "0", rate=D(rate)),
        buy_fee_mode=mode,  # type: ignore[arg-type]
    )


class FakeBooks:
    """In-memory BookSource. Books are re-stamped with the clock time unless pinned stale."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.books: dict[str, OrderBook] = {}
        self.pinned: set[str] = set()
        self.calls = 0

    def set(self, b: OrderBook, *, pin: bool = False) -> None:
        self.books[b.token_id] = b
        if pin:
            self.pinned.add(b.token_id)
        else:
            self.pinned.discard(b.token_id)

    async def get_book(self, token_id: str, *, fresh: bool = False) -> OrderBook | None:
        self.calls += 1
        b = self.books.get(token_id)
        if b is None or token_id in self.pinned:
            return b
        return b.model_copy(update={"received_at": self.clock.now()})
