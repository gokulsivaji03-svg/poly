"""Single-leader election: only one process may ever run the trading workers."""

import asyncio

from app.core.clock import ManualClock
from app.services.workers import Supervisor
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW


async def eventually(predicate, seconds=8.0):
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def counting_worker(runs: list[str], name: str):
    async def work(stop: asyncio.Event) -> None:
        runs.append(name)
        await stop.wait()

    return work


async def test_only_one_supervisor_leads_and_a_standby_takes_over(sm):
    runs: list[str] = []
    engines = [create_async_engine(TEST_DB) for _ in range(2)]
    a, b = (Supervisor(e, ManualClock(NOW)) for e in engines)
    a.add("copy", counting_worker(runs, "a"))
    b.add("copy", counting_worker(runs, "b"))
    try:
        await a.start()
        await eventually(lambda: a.is_leader)
        await b.start()
        await asyncio.sleep(0.3)
        assert (a.is_leader, b.is_leader) == (True, False)
        assert runs == ["a"], "the standby never runs workers"
        await a.stop()
        await eventually(lambda: b.is_leader)
        await eventually(lambda: runs == ["a", "b"])
    finally:
        await a.stop()
        await b.stop()
        for e in engines:
            await e.dispose()


async def test_a_crashing_worker_is_restarted_and_its_error_recorded(sm):
    attempts = 0

    async def flaky(stop: asyncio.Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        await stop.wait()

    engine = create_async_engine(TEST_DB)
    sup = Supervisor(engine, ManualClock(NOW))
    sup.add("flaky", flaky)
    try:
        await sup.start()
        await eventually(lambda: attempts == 2)
        status = sup.status["flaky"]
        assert status.restarts == 1 and status.last_error == "RuntimeError: boom"
        assert status.state == "running"
    finally:
        await sup.stop()
        await engine.dispose()
