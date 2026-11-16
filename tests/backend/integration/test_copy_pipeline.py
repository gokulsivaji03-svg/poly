"""End-to-end copy pipeline against a deterministic mocked Polymarket."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.db.models import CopySignal, SignalStatus, SourceEvent
from scenario import NO, WALLET_A, WALLET_B, YES, FakeRest, World, activity, make_runtime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW

D = Decimal


@pytest.fixture
def world():
    return World()


@pytest.fixture
async def env(sm, world):
    """Runtime wired to the mocked world. Fresh DB per test (``sm`` truncates)."""
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
        yield rt, clock, rest
        await rt.stop()


async def add_and_snapshot(rt, address, *, value="4000", cash=6000, world=None):
    world.value[address] = value
    world.cash[address] = cash
    w = await rt.registry.add(address, nickname=address[-4:])
    await rt.estimator.snapshot(w)
    return await rt.registry.get(w.id)


async def signals(rt):
    async with rt.sm() as s:
        return list((await s.scalars(select(CopySignal).order_by(CopySignal.id))).all())


async def test_proportional_copy_end_to_end(env, world):
    rt, clock, _ = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(30)
    # Source with ~$10,000 equity buys $200 (2%). User has $500 -> $10 target.
    world.activity[WALLET_A] = [
        activity(
            WALLET_A, ts=clock.now() - timedelta(seconds=5), size="500", price="0.40", tx="0xaa"
        )
    ]
    result = await rt.monitor.poll_once(w)
    assert result.new_trades == 1
    [sig] = await signals(rt)
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    assert sig.source_alloc_pct == D("0.02")
    assert sig.target_budget == D("10")
    assert sig.filled_shares == D("25")
    assert sig.filled_cost == D("10")
    assert sig.detection_lag_ms == 5000
    view = await rt.copy.portfolio()
    assert view.cash == D("490")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_duplicate_and_out_of_order_events(env, world):
    rt, clock, _ = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(120)  # late trade below must still postdate followed_since
    t0 = clock.now() - timedelta(seconds=3)
    row = activity(WALLET_A, ts=t0, tx="0x01")
    world.activity[WALLET_A] = [row, dict(row)]  # two identical fills in one tx
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    assert len(await signals(rt)) == 2  # identical fills are distinct events (ordinal)
    # Repeat the same page plus a late-indexed older trade (out of order).
    late = activity(WALLET_A, ts=t0 - timedelta(seconds=60), tx="0x00", size="50")
    world.activity[WALLET_A] = [row, dict(row), late]
    clock.advance(400)  # the late trade is now older than max_detection_delay (300s)
    res = await rt.monitor.poll_once(await rt.registry.get(w.id))
    assert res.new_events == 1
    sigs = await signals(rt)
    assert len(sigs) == 3
    assert sigs[-1].status == SignalStatus.REJECTED
    assert sigs[-1].reasons["items"][-1]["code"] == "STALE_SIGNAL"
    async with rt.sm() as s:
        assert await s.scalar(select(func.count()).select_from(SourceEvent)) == 3


async def test_non_trade_activity_is_recorded_not_copied(env, world):
    rt, clock, _ = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(10)
    ts = clock.now() - timedelta(seconds=2)
    world.activity[WALLET_A] = [
        activity(WALLET_A, ts=ts, typ="REDEEM", side=None, tx="0x10"),
        activity(WALLET_A, ts=ts, typ="SPLIT", side=None, tx="0x11"),
        activity(WALLET_A, ts=ts, typ="MERGE", side=None, tx="0x12"),
    ]
    res = await rt.monitor.poll_once(w)
    assert res.new_events == 3 and res.new_trades == 0
    assert await signals(rt) == []


async def test_backfill_is_history_only(env, world):
    rt, _clock, _ = env
    world.activity[WALLET_A] = [activity(WALLET_A, ts=NOW - timedelta(days=1), tx="0xold")]
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    res = await rt.monitor.poll_once(w)
    assert res.new_trades == 1  # recorded for history/metrics...
    assert await signals(rt) == []  # ...but never turned into a copy decision
    async with rt.sm() as s:
        assert (await s.scalar(select(SourceEvent.is_backfill))) is True


async def test_restart_recovers_missed_trades_once(world, sm):
    clock = ManualClock(NOW)
    rest = FakeRest(clock)
    rest.set(YES, asks=[("0.40", "1000")])
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        rt = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        await rt.settings.update({"paper": {"execution_delay_ms": 0}})
        w = await add_and_snapshot(rt, WALLET_A, world=world)
        clock.advance(20)
        world.activity[WALLET_A] = [activity(WALLET_A, ts=clock.now(), tx="0x1")]
        await rt.monitor.poll_once(w)
        await rt.stop()
        # Downtime: two more trades happen.
        clock.advance(60)
        world.activity[WALLET_A] += [
            activity(WALLET_A, ts=clock.now() - timedelta(seconds=30), tx="0x2"),
            activity(WALLET_A, ts=clock.now() - timedelta(seconds=10), tx="0x3"),
        ]
        rt2 = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        await rt2.recover()
        res = await rt2.monitor.poll_once(await rt2.registry.get(w.id))
        assert res.new_events == 2
        sigs = await signals(rt2)
        assert len(sigs) == 3
        assert all(sg.status == SignalStatus.EXECUTED for sg in sigs)
        assert await rt2.ledger.verify(rt2.account_id) == []
        await rt2.stop()


async def test_source_sell_larger_than_copied_inventory(env, world):
    rt, clock, _rest = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(10)
    world.activity[WALLET_A] = [activity(WALLET_A, ts=clock.now(), tx="0xb")]
    await rt.monitor.poll_once(w)  # we hold 25 YES
    clock.advance(10)
    world.positions[WALLET_A] = []  # source fully exited
    world.activity[WALLET_A].append(
        activity(WALLET_A, ts=clock.now(), side="SELL", size="5000", tx="0xs")
    )
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    sell = (await signals(rt))[-1]
    assert sell.side == "SELL" and sell.status == SignalStatus.EXECUTED, sell.reasons
    assert sell.filled_shares == D("25")
    assert await rt.ledger.open_lots(rt.account_id) == []
    assert await rt.ledger.verify(rt.account_id) == []


async def test_unknown_source_bankroll_skips(env, world):
    rt, clock, _ = env
    world.value[WALLET_A] = None  # value endpoint down
    w = await rt.registry.add(WALLET_A)
    await rt.estimator.snapshot(w)
    clock.advance(5)
    world.activity[WALLET_A] = [activity(WALLET_A, ts=clock.now(), tx="0xu")]
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    [sig] = await signals(rt)
    assert sig.status == SignalStatus.SKIPPED
    assert sig.reasons["items"][-1]["code"] == "SOURCE_EQUITY_UNKNOWN"
    assert sig.source_equity_coverage == "UNAVAILABLE"


async def test_simultaneous_trades_never_overspend(env, world):
    rt, clock, _ = env
    await rt.settings.update(
        {
            "sizing": {"mode": "FIXED_AMOUNT", "fixed_amount_usd": "100"},
            "risk": {
                "max_pct_per_trade": "1",
                "max_position_usd": "10000",
                "max_market_exposure_usd": "10000",
                "max_total_exposure_pct": "1",
                "min_cash_reserve_usd": "0",
            },
        }
    )
    wa = await add_and_snapshot(rt, WALLET_A, world=world)
    wb = await add_and_snapshot(rt, WALLET_B, world=world)
    clock.advance(5)
    for addr in (WALLET_A, WALLET_B):
        world.activity[addr] = [
            activity(addr, ts=clock.now(), tx=f"0x{addr[-2:]}{i}", size=str(100 + i))
            for i in range(4)
        ]
    await asyncio.gather(rt.monitor.poll_once(wa), rt.monitor.poll_once(wb))
    sigs = await signals(rt)
    executed = [sg for sg in sigs if sg.status == SignalStatus.EXECUTED]
    rejected = [sg for sg in sigs if sg.status == SignalStatus.REJECTED]
    assert len(sigs) == 8
    assert sum((sg.filled_cost for sg in executed), D(0)) <= D("500")
    assert len(executed) <= 5 and rejected
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash >= 0 and acct.reserved == 0
    assert await rt.ledger.verify(rt.account_id) == []


async def test_upstream_outages_are_retried_and_recorded(env, world):
    rt, clock, _ = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(5)
    world.activity[WALLET_A] = [activity(WALLET_A, ts=clock.now(), tx="0xr")]
    world.activity_errors = 2  # two 503s, then success (within retry budget)
    res = await rt.monitor.poll_once(w)
    assert res.new_trades == 1
    world.activity_errors = 10  # exceeds retries -> poll fails, nothing lost
    clock.advance(5)
    world.activity[WALLET_A].append(activity(WALLET_A, ts=clock.now(), tx="0xr2"))
    from app.integrations.http import UpstreamError

    with pytest.raises(UpstreamError):
        await rt.monitor.poll_once(await rt.registry.get(w.id))
    await rt.monitor._record_failure(await rt.registry.get(w.id), RuntimeError("503"))
    assert (await rt.registry.get(w.id)).last_error == "503"
    world.activity_errors = 0
    res = await rt.monitor.poll_once(await rt.registry.get(w.id))
    assert res.new_trades == 1
    assert (await rt.registry.get(w.id)).last_error is None


async def test_paused_and_unfollowed_are_respected(env, world):
    rt, clock, _ = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    await rt.settings.update({"strategies_paused": True})
    clock.advance(5)
    world.activity[WALLET_A] = [activity(WALLET_A, ts=clock.now(), tx="0xp")]
    await rt.monitor.poll_once(w)
    assert (await signals(rt))[-1].reasons["items"][-1]["code"] == "TRADING_HALTED"
    await rt.settings.update({"strategies_paused": False})
    await rt.registry.update(w.id, {"follow": False})
    clock.advance(5)
    world.activity[WALLET_A].append(activity(WALLET_A, ts=clock.now(), tx="0xq"))
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    assert (await signals(rt))[-1].reasons["items"][-1]["code"] == "NOT_FOLLOWED"
