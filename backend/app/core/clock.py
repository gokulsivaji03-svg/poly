"""Injectable clocks so services stay deterministic in tests and historical replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """Clock advanced explicitly; ``sleep`` advances time instantly (replay/tests)."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock requires an aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value < self._now:
            raise ValueError("ManualClock cannot move backwards")
        self._now = value

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
