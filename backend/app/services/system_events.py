"""Audit log for errors, reconciliation findings and operational events."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import SystemEvent
from app.services.event_bus import EventBus

log = logging.getLogger(__name__)


class SystemEventLog:
    def __init__(self, sm: async_sessionmaker[AsyncSession], bus: EventBus, clock: Clock) -> None:
        self._sm = sm
        self._bus = bus
        self._clock = clock

    async def record(
        self, kind: str, message: str, *, level: str = "INFO", data: dict[str, Any] | None = None
    ) -> None:
        getattr(log, level.lower(), log.info)(message, extra={"kind": kind, **(data or {})})
        try:
            async with self._sm() as s, s.begin():
                row = SystemEvent(
                    level=level,
                    kind=kind,
                    message=message,
                    data=data or {},
                    created_at=self._clock.now(),
                )
                s.add(row)
                await s.flush()
                payload = {
                    "id": row.id,
                    "level": level,
                    "kind": kind,
                    "message": message,
                    "data": data or {},
                    "created_at": row.created_at.isoformat(),
                }
        except Exception:  # the audit log must never take a worker down
            log.exception("failed to persist system event", extra={"kind": kind})
            return
        self._bus.publish("system", payload)
