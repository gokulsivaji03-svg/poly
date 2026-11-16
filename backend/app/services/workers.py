"""Background worker lifecycle with single-leader election.

Workers run only in the process that holds a PostgreSQL session-level advisory lock, so
uvicorn ``--reload``, multiple workers or extra replicas can never double-trade. Standby
processes keep retrying the lock and take over if the leader dies (its session ends and
PostgreSQL releases the lock).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.clock import Clock

log = logging.getLogger(__name__)

LEADER_LOCK_KEY = 0x504F4C59  # "POLY"

WorkerFn = Callable[[asyncio.Event], Awaitable[None]]


@dataclass
class WorkerStatus:
    state: str = "standby"
    last_run_at: datetime | None = None
    last_error: str | None = None
    restarts: int = 0


@dataclass
class Supervisor:
    engine: AsyncEngine
    clock: Clock
    workers: dict[str, WorkerFn] = field(default_factory=dict)
    status: dict[str, WorkerStatus] = field(default_factory=dict)
    is_leader: bool = False
    on_leader: Callable[[], Awaitable[None]] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None
    _lock_conn: AsyncConnection | None = None

    def add(self, name: str, fn: WorkerFn) -> None:
        self.workers[name] = fn
        self.status[name] = WorkerStatus()

    def heartbeat(self, name: str) -> None:
        st = self.status.get(name)
        if st is not None:
            st.last_run_at = self.clock.now()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="supervisor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._lock_conn is not None:
            with contextlib.suppress(Exception):
                await self._lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": LEADER_LOCK_KEY}
                )
                await self._lock_conn.close()
            self._lock_conn = None
        self.is_leader = False

    async def _acquire(self) -> bool:
        conn = await self.engine.connect()
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK_KEY})
        ).scalar()
        await conn.commit()
        if got:
            self._lock_conn = conn
            return True
        await conn.close()
        return False

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if await self._acquire():
                    break
            except Exception as exc:
                log.warning("leader lock attempt failed", extra={"error": str(exc)})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)
        if self._stop.is_set():
            return
        self.is_leader = True
        log.info("acquired strategy leader lock; starting workers")
        if self.on_leader is not None:
            await self.on_leader()
        tasks = [
            asyncio.create_task(self._keep(name, fn), name=f"worker-{name}")
            for name, fn in self.workers.items()
        ]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for st in self.status.values():
            st.state = "stopped"

    async def _keep(self, name: str, fn: WorkerFn) -> None:
        """Run a worker; restart it with backoff if it crashes (never silently die)."""
        st = self.status[name]
        delay = 1.0
        while not self._stop.is_set():
            st.state = "running"
            try:
                await fn(self._stop)
                if self._stop.is_set():
                    break
                st.last_error = "worker exited unexpectedly"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("worker crashed", extra={"worker": name})
                st.last_error = f"{type(exc).__name__}: {exc}"
            st.state = "error"
            st.restarts += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, 60)
        st.state = "stopped"


async def periodic(
    stop: asyncio.Event,
    interval_s: float,
    fn: Callable[[], Awaitable[None]],
    *,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Run ``fn`` every ``interval_s`` until ``stop``; errors propagate to the supervisor."""
    while not stop.is_set():
        await fn()
        if on_tick is not None:
            on_tick()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
