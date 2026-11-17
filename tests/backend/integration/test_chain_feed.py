"""Polygon trade feed: mapping, push/backstop dedupe, restart cursor, reorgs and dispatch."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.db.models import CopySignal, FeedCursor, SignalStatus, SourceEvent, SystemEvent
from scenario import (
    NO,
    WALLET_A,
    WALLET_B,
    YES,
    FakeRest,
    World,
    activity,
    fill_log,
    make_runtime,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW, market

D = Decimal


@pytest.fixture
def world():
    return World()


@pytest.fixture
async def env(sm, world):
    clock = ManualClock(NOW)
    rest = FakeRest(clock)
    rest.set(YES, asks=[("0.40", "1000")], bids=[("0.39", "1000")])
    rest.set(NO, asks=[("0.62", "1000")], bids=[("0.60", "1000")])
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        rt = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        await rt.settings.update(
            {"paper": {"starting_bankroll_usd": "500", "execution_delay_ms": 0}}
        )
        await rt.ledger.reset_account(rt.account_id, D("500"))
        world.value[WALLET_A] = "4000"
        world.cash[WALLET_A] = 6000
        w = await rt.registry.add(WALLET_A, nickname="a")
        await rt.estimator.snapshot(w)
        clock.advance(30)
        yield rt, clock, world, await rt.registry.get(w.id), rest
        await rt.stop()


async def rows(rt, model):
    async with rt.sm() as s:
        return list((await s.scalars(select(model).order_by(model.id))).all())


def pushed(result):
    return {"jsonrpc": "2.0", "method": "eth_subscription", "params": {"result": result}}


def append_fill(world, clock, *, tx="0xabc", token=YES, block=None, ago=2, **kw):
    block = block or world.head + 1
    world.head = max(world.head, block)
    world.block_ts[block] = int((clock.now() - timedelta(seconds=ago)).timestamp())
    log = fill_log(WALLET_A, block=block, tx=tx, token=token, **kw)
    world.fill_logs.append(log)
    return log


async def test_backstop_copies_fill_once_and_records_chain_values(env):
    rt, clock, world, _w, _rest = env
    append_fill(world, clock)
    assert await rt.feed.tick() == 1
    await rt.feed.drain()
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    [sig] = await rows(rt, CopySignal)
    assert sig.status == SignalStatus.EXECUTED and sig.detection_lag_ms == 2000
    [ev] = await rows(rt, SourceEvent)
    assert ev.source == "chain" and (ev.size, ev.price) == (D("500"), D("0.4"))


async def test_imported_history_then_same_chain_fill_is_not_copied(env):
    rt, clock, world, w, _rest = env
    old = clock.now() - timedelta(days=1)
    world.activity[WALLET_A] = [activity(WALLET_A, ts=old, tx="0xabc")]
    assert await rt.observer.import_history(w) == 1
    append_fill(world, clock, ago=24 * 60 * 60, tx="0xabc")
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    [ev] = await rows(rt, SourceEvent)
    assert ev.source == "api" and ev.is_backfill is True
    assert await rows(rt, CopySignal) == []


async def test_unmapped_fill_retries_then_ingests_when_gamma_maps_token(env, monkeypatch):
    rt, clock, world, _w, _rest = env
    mapped = False

    async def fetch_by_token(token):
        return market(tokens=("999", NO)) if mapped and token == "999" else None

    monkeypatch.setattr(rt.feed._meta, "fetch_by_token", fetch_by_token)
    append_fill(world, clock, token="999")
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    mapped = True
    clock.advance(1)
    assert await rt.feed.tick() == 1
    await rt.feed.drain()
    [ev] = await rows(rt, SourceEvent)
    assert ev.token_id == "999"


async def test_unmapped_fill_is_reported_after_retry_ttl(env):
    rt, clock, world, _w, _rest = env
    append_fill(world, clock, token="999", tx="0xdrop")
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    clock.advance(601)
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    events = await rows(rt, SystemEvent)
    assert events[-1].kind == "UNMAPPED_FILL_DROPPED"


async def test_cursor_persists_across_runtime_restart(env):
    rt, clock, world, _w, rest = env
    append_fill(world, clock, tx="0x1")
    assert await rt.feed.tick() == 1
    await rt.feed.drain()
    async with rt.sm() as s:
        cursor = await s.get(FeedCursor, "polygon_fills")
        assert cursor is not None and cursor.next_block == world.head + 1
    await rt.stop()
    rt2 = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
    try:
        assert await rt2.feed.tick() == 0
        await rt2.feed.drain()
        append_fill(world, clock, tx="0x2")
        assert await rt2.feed.tick() == 1
        await rt2.feed.drain()
        assert [e.tx_hash for e in await rows(rt2, SourceEvent)] == ["0x1", "0x2"]
    finally:
        await rt2.stop()


async def test_pushed_fill_needs_no_rpc_and_backstop_deduplicates(env):
    rt, clock, world, _w, _rest = env
    await rt.feed.tick()
    await rt.feed.drain()
    calls_before = sum(world.rpc_calls.values())
    block = world.head + 1
    ts = int((clock.now() - timedelta(seconds=1)).timestamp())
    await rt.feed.handle_message(pushed({"number": hex(block), "timestamp": hex(ts)}))
    await rt.feed.drain()
    log = fill_log(WALLET_A, block=block, tx="0xpush")
    assert await rt.feed.handle_message(pushed(log)) == 1
    await rt.feed.drain()
    assert sum(world.rpc_calls.values()) == calls_before
    world.head, world.block_ts[block] = block, ts
    world.fill_logs.append(log)
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    assert len(await rows(rt, CopySignal)) == 1


async def test_removed_and_unfollowed_pushes_are_ignored(env):
    rt, clock, _world, _w, _rest = env
    await rt.feed.tick()
    await rt.feed.drain()
    block = 1005
    ts = int(clock.now().timestamp())
    await rt.feed.handle_message(pushed({"number": hex(block), "timestamp": hex(ts)}))
    await rt.feed.drain()
    reorged = {**fill_log(WALLET_A, block=block, tx="0xr1"), "removed": True}
    stranger = fill_log("0x" + "99" * 20, block=block, tx="0xr2")
    assert await rt.feed.handle_message(pushed(reorged)) == 0
    await rt.feed.drain()
    assert await rt.feed.handle_message(pushed(stranger)) == 0
    await rt.feed.drain()
    assert await rows(rt, SourceEvent) == []


async def test_push_does_not_wait_for_copy_and_keeps_wallet_order(env, monkeypatch):
    rt, clock, _world, _w, _rest = env
    await rt.registry.add(WALLET_B, nickname="b")
    await rt.feed.tick()
    release = asyncio.Event()
    started: list[str] = []

    async def on_trade(ev, wallet):
        started.append(ev.tx_hash)
        if ev.tx_hash == "0xa1":
            await release.wait()

    monkeypatch.setattr(rt.feed._engine, "on_trade", on_trade)
    block = 1100
    await rt.feed.handle_message(
        pushed({"number": hex(block), "timestamp": hex(int(clock.now().timestamp()))})
    )
    for wallet, tx, index in ((WALLET_A, "0xa1", 0), (WALLET_A, "0xa2", 1), (WALLET_B, "0xb1", 2)):
        log = fill_log(wallet, block=block, tx=tx, log_index=index)
        assert await asyncio.wait_for(rt.feed.handle_message(pushed(log)), timeout=2) == 1
    for _ in range(20):
        await asyncio.sleep(0)
    assert started == ["0xa1", "0xb1"], "B copies while A is busy; A's next fill waits"
    release.set()
    await rt.feed.drain()
    assert started == ["0xa1", "0xb1", "0xa2"]
