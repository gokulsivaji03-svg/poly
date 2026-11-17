"""On-chain fill detection: copies once, whichever of chain or Data API sees a trade first."""

from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.db.models import CopySignal, SignalStatus, SourceEvent
from scenario import NO, WALLET_A, YES, FakeRest, World, activity, fill_log, make_runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW

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
        engine = create_async_engine(TEST_DB)
        rt = await make_runtime(TEST_DB, clock, rest, engine)
        await rt.settings.update(
            {"paper": {"starting_bankroll_usd": "500", "execution_delay_ms": 0}}
        )
        await rt.ledger.reset_account(rt.account_id, D("500"))
        world.value[WALLET_A] = "4000"
        world.cash[WALLET_A] = 6000
        w = await rt.registry.add(WALLET_A, nickname="a")
        await rt.estimator.snapshot(w)
        clock.advance(30)
        yield rt, clock, world, await rt.registry.get(w.id)
        await rt.stop()


async def rows(rt, model):
    async with rt.sm() as s:
        return list((await s.scalars(select(model).order_by(model.id))).all())


def chain_trade(world, clock, *, tx="0xabc", token=YES, block=1001, ago=2):
    world.head = block
    world.block_ts[block] = int((clock.now() - timedelta(seconds=ago)).timestamp())
    world.fill_logs.append(fill_log(WALLET_A, block=block, tx=tx, token=token))
    return clock.now() - timedelta(seconds=ago)


async def test_chain_fill_is_copied_within_seconds_and_api_duplicate_is_ignored(env):
    rt, clock, world, w = env
    assert rt.chain is not None
    ts = chain_trade(world, clock)
    assert await rt.chain.tick() == 1
    [sig] = await rows(rt, CopySignal)
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    assert sig.detection_lag_ms == 2000
    [ev] = await rows(rt, SourceEvent)
    assert ev.source == "chain" and (ev.size, ev.price) == (D("500"), D("0.4"))
    # The Data API reports the same trade later, with differently rounded cash.
    world.activity[WALLET_A] = [activity(WALLET_A, ts=ts, tx="0xabc", usdc="199.999999")]
    clock.advance(10)
    result = await rt.monitor.poll_once(w)
    assert result.new_trades == 0
    assert len(await rows(rt, CopySignal)) == 1


async def test_api_first_then_chain_duplicate_is_ignored(env):
    rt, clock, world, w = env
    assert rt.chain is not None
    ts = clock.now() - timedelta(seconds=2)
    world.activity[WALLET_A] = [activity(WALLET_A, ts=ts, tx="0xabc", usdc="199.999999")]
    await rt.monitor.poll_once(w)
    chain_trade(world, clock)
    assert await rt.chain.tick() == 0
    sigs = await rows(rt, CopySignal)
    assert len(sigs) == 1 and sigs[0].status == SignalStatus.EXECUTED
    assert {e.source for e in await rows(rt, SourceEvent)} == {"api"}


async def test_unmapped_token_is_left_to_the_data_api(env):
    rt, clock, world, _w = env
    assert rt.chain is not None
    chain_trade(world, clock, token="999")  # not an outcome of any market Gamma returns
    assert await rt.chain.tick() == 0
    assert await rows(rt, SourceEvent) == []


async def test_blocks_are_processed_once_and_in_order(env):
    rt, clock, world, _w = env
    assert rt.chain is not None
    chain_trade(world, clock, tx="0x1", block=1001)
    assert await rt.chain.tick() == 1
    assert await rt.chain.tick() == 0  # same head: nothing re-read
    chain_trade(world, clock, tx="0x2", block=1002, ago=1)
    assert await rt.chain.tick() == 1
    assert [e.tx_hash for e in await rows(rt, SourceEvent)] == ["0x1", "0x2"]


def pushed(result):
    return {"jsonrpc": "2.0", "method": "eth_subscription", "params": {"result": result}}


async def test_pushed_fill_is_copied_without_any_rpc_round_trip(env):
    rt, clock, world, _w = env
    assert rt.chain is not None
    await rt.chain.tick()  # learns the followed wallets, as the running worker does
    calls_before = sum(world.rpc_calls.values())
    ts = int((clock.now() - timedelta(seconds=1)).timestamp())
    await rt.chain.handle_message(pushed({"number": hex(1005), "timestamp": hex(ts)}))
    lg = fill_log(WALLET_A, block=1005, tx="0xpush")
    assert await rt.chain.handle_message(pushed(lg)) == 1
    assert sum(world.rpc_calls.values()) == calls_before  # timestamp came from newHeads
    [sig] = await rows(rt, CopySignal)
    assert sig.status == SignalStatus.EXECUTED and sig.detection_lag_ms == 1000
    # The backstop poll re-reads the same block: no second copy.
    world.head, world.block_ts[1005] = 1005, ts
    world.fill_logs.append(lg)
    assert await rt.chain.tick() == 0
    assert len(await rows(rt, CopySignal)) == 1


async def test_removed_and_unfollowed_pushes_are_ignored(env):
    rt, clock, _world, _w = env
    assert rt.chain is not None
    await rt.chain.tick()
    ts = int(clock.now().timestamp())
    await rt.chain.handle_message(pushed({"number": hex(1005), "timestamp": hex(ts)}))
    reorged = {**fill_log(WALLET_A, block=1005, tx="0xr1"), "removed": True}
    stranger = fill_log("0x" + "99" * 20, block=1005, tx="0xr2")
    assert await rt.chain.handle_message(pushed(reorged)) == 0
    assert await rt.chain.handle_message(pushed(stranger)) == 0
    assert await rt.chain.handle_message({"jsonrpc": "2.0", "id": 2, "result": "0xsub"}) == 0
    assert await rows(rt, SourceEvent) == []
