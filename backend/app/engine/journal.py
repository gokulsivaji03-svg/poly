"""Copy-signal journal: one auditable ``copy_signals`` row per source trade.

The row is opened before any decision (unique per source event, so reprocessing is
idempotent) and finished exactly once with its status, every reason in order, and the fill.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.money import ZERO
from app.core.stages import StageTimer
from app.db.models import CopySignal, Order, OrderStatus, SignalStatus, SourceEvent
from app.domain.reasons import Reason
from app.execution.orders import ExecutionResult
from app.services.event_bus import EventBus

_STATUS = {
    OrderStatus.FILLED: SignalStatus.EXECUTED,
    OrderStatus.PARTIALLY_FILLED: SignalStatus.PARTIAL,
    OrderStatus.REJECTED: SignalStatus.REJECTED,
    OrderStatus.FAILED: SignalStatus.FAILED,
}


def ms_between(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


class SignalJournal:
    def __init__(self, sm: async_sessionmaker[AsyncSession], bus: EventBus, clock: Clock) -> None:
        self._sm = sm
        self._bus = bus
        self._clock = clock

    async def open(self, ev: SourceEvent) -> CopySignal:
        async with self._sm() as s, s.begin():
            sig = await s.scalar(
                pg_insert(CopySignal)
                .values(
                    source_event_id=ev.id,
                    wallet_id=ev.wallet_id,
                    status=SignalStatus.PENDING,
                    side=ev.side or "",
                    condition_id=ev.condition_id,
                    token_id=ev.token_id,
                    source_price=ev.price,
                    source_shares=ev.size,
                    source_cost=ev.usdc_size if ev.usdc_size > ZERO else ev.size * ev.price,
                    reasons={"items": []},
                    detected_at=ev.detected_at,
                    detection_lag_ms=ms_between(ev.block_ts, ev.detected_at),
                )
                .on_conflict_do_nothing(index_elements=["source_event_id"])
                .returning(CopySignal)
            )
            if sig is None:  # reprocessing: the signal was opened before
                sig = await s.scalar(select(CopySignal).where(CopySignal.source_event_id == ev.id))
            assert sig is not None
        return sig

    async def save(self, sig: CopySignal, **fields: Any) -> CopySignal:
        async with self._sm() as s, s.begin():
            row = await s.get(CopySignal, sig.id, with_for_update=True)
            assert row is not None
            for k, v in fields.items():
                setattr(row, k, v)
        self._bus.publish("copy_signal", {"id": row.id})
        return row

    async def finish(
        self,
        sig: CopySignal,
        status: SignalStatus,
        reasons: list[Reason],
        *,
        timer: StageTimer | None = None,
        **fields: Any,
    ) -> CopySignal:
        decided_at = fields.pop("decided_at", None) or self._clock.now()
        if timer is not None:
            fields["stage_us"] = timer.as_us()
        return await self.save(
            sig,
            status=status,
            decided_at=decided_at,
            signal_latency_ms=ms_between(sig.detected_at, decided_at),
            reasons={"items": [{"code": r.code, "message": r.message} for r in reasons]},
            **fields,
        )

    async def record_execution(
        self,
        sig: CopySignal,
        result: ExecutionResult,
        reasons: list[Reason],
        decided_at: datetime,
        timer: StageTimer,
        **decision: Any,
    ) -> CopySignal:
        """Finish ``sig`` with its execution and the ``decision`` fields it was made with.

        The decision is written here, once, rather than before execution: a crash in between
        leaves the signal PENDING, and recovery re-decides while the order's idempotency key
        returns the original fill."""
        reasons = list(reasons)
        if result.status is OrderStatus.PARTIALLY_FILLED:
            reasons.append(
                Reason("PARTIAL_FILL", "Book depth within limit exhausted; remainder cancelled.")
            )
        if result.reason and not result.filled:
            code = result.reason.split()[0].split(":")[0]
            reasons.append(Reason("EXECUTION_" + code, result.reason))
        async with self._sm() as s:
            order = await s.get(Order, result.order_id)
        fill = result.fill if result.filled else None
        if fill is not None:
            self._bus.publish("portfolio", {})
            self._bus.publish("positions", {})
        return await self.finish(
            sig,
            _STATUS.get(result.status, SignalStatus.FAILED),
            reasons,
            timer=timer,
            decided_at=decided_at,
            **decision,
            order_id=result.order_id,
            executed_at=self._clock.now() if fill else None,
            filled_shares=fill.net_shares if fill else None,
            filled_cost=fill.cash_cost if fill else None,
            avg_fill_price=fill.avg_price if fill else None,
            exec_latency_ms=order.exec_latency_ms if order else None,
        )
