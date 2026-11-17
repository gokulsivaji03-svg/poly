"""ORM tables. Constraints encode the accounting invariants the services rely on."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AccountMode(StrEnum):
    PAPER = "PAPER"
    REPLAY = "REPLAY"
    LIVE = "LIVE"


class OrderKind(StrEnum):
    COPY_BUY = "COPY_BUY"
    COPY_SELL = "COPY_SELL"
    HEDGE_BUY = "HEDGE_BUY"
    PAIR_UNWIND = "PAIR_UNWIND"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    """Immediate-or-cancel semantics: the unfilled remainder was cancelled."""
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class LotOrigin(StrEnum):
    COPY = "COPY"
    HEDGE = "HEDGE"


class EntryKind(StrEnum):
    DEPOSIT = "DEPOSIT"
    BUY = "BUY"
    SELL = "SELL"
    SETTLEMENT = "SETTLEMENT"
    MERGE = "MERGE"


class ReservationStatus(StrEnum):
    OPEN = "OPEN"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


def _now() -> Any:
    return func.now()


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("reserved >= 0", name="reserved_nonneg"),
        CheckConstraint("cash >= 0", name="cash_nonneg"),
        CheckConstraint("reserved <= cash", name="reserved_le_cash"),
        UniqueConstraint("mode", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64), default="default")
    starting_bankroll: Mapped[Decimal]
    cash: Mapped[Decimal]
    reserved: Mapped[Decimal]
    realized_pnl: Mapped[Decimal]
    fees_paid: Mapped[Decimal]
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now(), onupdate=_now())


class CashReservation(Base):
    __tablename__ = "cash_reservations"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_pos"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(16), default=ReservationStatus.OPEN)
    purpose: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    closed_at: Mapped[datetime | None]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    mode: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(100), index=True)
    requested_budget: Mapped[Decimal | None]
    requested_shares: Mapped[Decimal | None]
    limit_price: Mapped[Decimal | None]
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING)
    reason: Mapped[str | None] = mapped_column(Text)
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("cash_reservations.id"))
    signal_id: Mapped[int | None] = mapped_column(index=True)
    hedge_id: Mapped[int | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    completed_at: Mapped[datetime | None]
    book_age_ms: Mapped[int | None]
    exec_latency_ms: Mapped[int | None]

    fills: Mapped[list[Fill]] = relationship(back_populates="order", lazy="selectin")


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (
        CheckConstraint("gross_shares > 0", name="gross_pos"),
        CheckConstraint("net_shares > 0 AND net_shares <= gross_shares", name="net_le_gross"),
        CheckConstraint("fee_usdc >= 0", name="fee_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    side: Mapped[str] = mapped_column(String(4))
    condition_id: Mapped[str] = mapped_column(String(80))
    token_id: Mapped[str] = mapped_column(String(100), index=True)
    gross_shares: Mapped[Decimal]
    net_shares: Mapped[Decimal]
    notional: Mapped[Decimal]
    fee_usdc: Mapped[Decimal]
    cash_delta: Mapped[Decimal]
    avg_price: Mapped[Decimal]
    worst_price: Mapped[Decimal]
    legs: Mapped[dict[str, Any]]
    book_hash: Mapped[str | None] = mapped_column(String(80))
    book_received_at: Mapped[datetime | None]
    executed_at: Mapped[datetime]

    order: Mapped[Order] = relationship(back_populates="fills")


class Lot(Base):
    """An acquisition of outcome shares. Matched shares are paired with opposite-side lots."""

    __tablename__ = "lots"
    __table_args__ = (
        CheckConstraint("shares_remaining >= 0", name="remaining_nonneg"),
        CheckConstraint("shares_remaining <= shares_acquired", name="remaining_le_acquired"),
        CheckConstraint("matched_shares >= 0", name="matched_nonneg"),
        CheckConstraint("matched_shares <= shares_remaining", name="matched_le_remaining"),
        CheckConstraint("cost_remaining >= 0", name="cost_nonneg"),
        Index(
            "ix_lots_open",
            "account_id",
            "condition_id",
            "token_id",
            postgresql_where="shares_remaining > 0",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(100), index=True)
    outcome: Mapped[str] = mapped_column(String(64), default="")
    market_question: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(8))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    fill_id: Mapped[int] = mapped_column(ForeignKey("fills.id"))
    source_wallet: Mapped[str | None] = mapped_column(String(64), index=True)
    entry_price: Mapped[Decimal]
    """All-in cost per delivered share (cost_total / shares_acquired)."""
    shares_acquired: Mapped[Decimal]
    shares_remaining: Mapped[Decimal]
    cost_total: Mapped[Decimal]
    cost_remaining: Mapped[Decimal]
    fee_usdc: Mapped[Decimal]
    matched_shares: Mapped[Decimal]
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]


class LotClosure(Base):
    __tablename__ = "lot_closures"
    __table_args__ = (CheckConstraint("shares > 0", name="shares_pos"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("fills.id"))
    shares: Mapped[Decimal]
    proceeds: Mapped[Decimal]
    cost_released: Mapped[Decimal]
    realized_pnl: Mapped[Decimal]
    closed_at: Mapped[datetime]


class LedgerEntry(Base):
    """Every cash movement. Invariant: account.cash == SUM(amount)."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal]
    cash_after: Mapped[Decimal]
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("fills.id"))
    memo: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(index=True)
    cash: Mapped[Decimal]
    reserved: Mapped[Decimal]
    positions_value: Mapped[Decimal]
    equity: Mapped[Decimal]
    realized_pnl: Mapped[Decimal]
    unrealized_pnl: Mapped[Decimal]
    locked_profit: Mapped[Decimal]
    unmarked_positions: Mapped[int] = mapped_column(default=0)
    """Open positions without a usable mark (valued at cost); surfaced as a data-quality flag."""


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int]
    document: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(server_default=_now(), onupdate=_now())


class SystemEvent(Base):
    """Errors, reconciliation findings, and operational events, kept for audit."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(48), index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(index=True)


# --------------------------------------------------------------------------- wallets
class SignalStatus(StrEnum):
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"
    """Not attempted: policy (unfollowed, backfill, sizing skip, non-copyable)."""
    REJECTED = "REJECTED"
    """Risk engine or execution refused it."""
    EXECUTED = "EXECUTED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class WatchedWallet(Base):
    __tablename__ = "watched_wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    follow: Mapped[bool] = mapped_column(default=True)
    sizing_override: Mapped[dict[str, Any] | None]
    added_at: Mapped[datetime]
    followed_since: Mapped[datetime]
    """Trades with block time before this are history only and are never copied."""
    removed_at: Mapped[datetime | None]
    last_polled_at: Mapped[datetime | None]
    """Last public observation attempt (equity snapshot)."""
    last_success_at: Mapped[datetime | None]
    last_trade_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(default=0)


class FeedCursor(Base):
    """Persisted position of a block-ordered feed, so a restart resumes without gaps."""

    __tablename__ = "feed_cursors"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_block: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime]


class SourceEvent(Base):
    __tablename__ = "source_events"
    __table_args__ = (Index("ix_source_events_wallet_ts", "wallet_id", "block_ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("watched_wallets.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    occurrence: Mapped[int] = mapped_column(default=0)
    type: Mapped[str] = mapped_column(String(16))
    raw_type: Mapped[str] = mapped_column(String(32))
    side: Mapped[str | None] = mapped_column(String(4))
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(64), default="")
    outcome_index: Mapped[int | None]
    size: Mapped[Decimal]
    usdc_size: Mapped[Decimal]
    price: Mapped[Decimal]
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    block_ts: Mapped[datetime]
    detected_at: Mapped[datetime] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    slug: Mapped[str] = mapped_column(String(255), default="")
    is_combo: Mapped[bool] = mapped_column(default=False)
    is_backfill: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(8), default="api", server_default="api")
    confirmation: Mapped[str | None] = mapped_column(String(10))
    """Mempool rows only: PENDING until their transaction lands, then CONFIRMED, REVERTED
    or DROPPED (never mined)."""
    """Where the row was first seen: ``chain`` (exchange logs) or ``api`` (history import)."""


class WalletEquitySnapshot(Base):
    __tablename__ = "wallet_equity_snapshots"
    __table_args__ = (Index("ix_wallet_equity_wallet_ts", "wallet_id", "observed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("watched_wallets.id"))
    observed_at: Mapped[datetime]
    positions_value: Mapped[Decimal | None]
    cash_balance: Mapped[Decimal | None]
    coverage: Mapped[str] = mapped_column(String(24))
    open_positions: Mapped[int | None]
    realized_pnl: Mapped[Decimal | None]
    unrealized_pnl: Mapped[Decimal | None]
    error: Mapped[str | None] = mapped_column(Text)


class WalletPosition(Base):
    """Latest observed position snapshot of a source wallet (replaced on each refresh)."""

    __tablename__ = "wallet_positions"
    __table_args__ = (UniqueConstraint("wallet_id", "token_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("watched_wallets.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(100))
    condition_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[Decimal]
    avg_price: Mapped[Decimal | None]
    current_price: Mapped[Decimal | None]
    current_value: Mapped[Decimal | None]
    realized_pnl: Mapped[Decimal | None]
    unrealized_pnl: Mapped[Decimal | None]
    updated_at: Mapped[datetime]


class CopySignal(Base):
    """Decision record for one source event: sizing inputs, risk reasons, execution outcome."""

    __tablename__ = "copy_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_event_id: Mapped[int] = mapped_column(ForeignKey("source_events.id"), unique=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("watched_wallets.id"), index=True)
    status: Mapped[str] = mapped_column(String(12), default=SignalStatus.PENDING, index=True)
    side: Mapped[str] = mapped_column(String(4))
    condition_id: Mapped[str] = mapped_column(String(80))
    token_id: Mapped[str] = mapped_column(String(100))
    source_price: Mapped[Decimal]
    source_shares: Mapped[Decimal]
    source_cost: Mapped[Decimal]
    source_equity: Mapped[Decimal | None]
    source_equity_coverage: Mapped[str | None] = mapped_column(String(24))
    source_confidence: Mapped[Decimal | None]
    source_alloc_pct: Mapped[Decimal | None]
    user_equity: Mapped[Decimal | None]
    proposed_pct: Mapped[Decimal | None]
    target_budget: Mapped[Decimal | None]
    approved_budget: Mapped[Decimal | None]
    limit_price: Mapped[Decimal | None]
    target_shares: Mapped[Decimal | None]
    filled_shares: Mapped[Decimal | None]
    filled_cost: Mapped[Decimal | None]
    avg_fill_price: Mapped[Decimal | None]
    reasons: Mapped[dict[str, Any]]
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    detected_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    executed_at: Mapped[datetime | None]
    detection_lag_ms: Mapped[int | None]
    signal_latency_ms: Mapped[int | None]
    exec_latency_ms: Mapped[int | None]
    stage_us: Mapped[dict[str, Any] | None]
    """Monotonic µs from fill receipt to each ``app.core.stages.Stage`` this signal reached."""


# ---------------------------------------------------------------------------- hedges
class HedgePosition(Base):
    """Hedge lifecycle for one (account, condition, primary outcome) position."""

    __tablename__ = "hedge_positions"
    __table_args__ = (UniqueConstraint("account_id", "condition_id", "primary_token_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    condition_id: Mapped[str] = mapped_column(String(80))
    primary_token_id: Mapped[str] = mapped_column(String(100))
    hedge_token_id: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(24))
    last_quote: Mapped[dict[str, Any] | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class HedgeEvent(Base):
    __tablename__ = "hedge_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hedge_position_id: Mapped[int] = mapped_column(ForeignKey("hedge_positions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str | None] = mapped_column(String(24))
    detail: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(index=True)


# ---------------------------------------------------------------------------- replay
class BookSnapshot(Base):
    """Recorded executable book; the only price source historical replay may use."""

    __tablename__ = "book_snapshots"
    __table_args__ = (Index("ix_book_snapshots_token_ts", "token_id", "received_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[str] = mapped_column(String(100))
    condition_id: Mapped[str] = mapped_column(String(80))
    received_at: Mapped[datetime]
    bids: Mapped[dict[str, Any]]
    asks: Mapped[dict[str, Any]]
    tick_size: Mapped[Decimal]
    min_order_size: Mapped[Decimal]
    hash: Mapped[str | None] = mapped_column(String(80))


class ReplayRun(Base):
    __tablename__ = "replay_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(12))
    params: Mapped[dict[str, Any]]
    result: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
