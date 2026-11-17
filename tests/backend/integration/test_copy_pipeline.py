"""End-to-end copy pipeline against a deterministic mocked Polymarket."""

from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from app.api import queries
from app.core.clock import ManualClock
from app.db.models import CopySignal, SignalStatus, SourceEvent, WalletEquitySnapshot
from scenario import (
    NO,
    WALLET_A,
    WALLET_B,
    YES,
    FakeRest,
    World,
    activity,
    chain_trade,
    fill_log,
    make_runtime,
)
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


async def test_observed_pnl_uses_account_pnl_not_lingering_positions(env, world):
    """Redeemed winners leave /v2/positions while worthless losers linger as REDEEMABLE."""
    rt, _, _ = env
    world.positions[WALLET_A] = [
        {
            "token_id": NO,
            "condition_id": "0xc",
            "current_size": 1000,
            "current_price": 0.0,
            "current_value": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": -600.0,
            "status": "REDEEMABLE",
        }
    ]
    world.pnl[WALLET_A] = ("358273.61", "17777.96")
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    async with rt.sm() as s:
        snap = await s.scalar(
            select(WalletEquitySnapshot).where(WalletEquitySnapshot.wallet_id == w.id)
        )
    assert snap is not None
    assert (snap.realized_pnl, snap.unrealized_pnl) == (D("358273.61"), D("17777.96"))


async def test_proportional_copy_end_to_end(env, world):
    rt, clock, _ = env
    await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(30)
    # Source with ~$10,000 equity buys $200 (2%). User has $500 -> $10 target.
    result = await chain_trade(
        rt,
        world,
        clock,
        WALLET_A,
        ago_s=5,
        size="500",
        price="0.40",
        tx="0xaa",
    )
    assert result == 1
    [sig] = await signals(rt)
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    assert sig.source_alloc_pct == D("0.02")
    assert sig.target_budget == D("10")
    assert sig.filled_shares == D("25")
    assert sig.filled_cost == D("10")
    assert sig.detection_lag_ms == 5000
    [row] = await queries.copy_signals(rt, limit=1)
    assert D(row["premium"]) == 0, "filled at the source's price"
    assert row["stages_ms"]["total"] > 0
    quality = await queries.copy_quality(rt, 24)
    assert (quality["filled"], D(quality["at_source_share"])) == (1, 1)
    view = await rt.portfolio.view()
    assert view.cash == D("490")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_duplicate_push_and_out_of_order_trade(env, world):
    rt, clock, _ = env
    await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(120)
    world.head += 1
    block = world.head
    world.block_ts[block] = int((clock.now() - timedelta(seconds=3)).timestamp())
    row = fill_log(WALLET_A, block=block, tx="0x01")
    pushed = {"jsonrpc": "2.0", "method": "eth_subscription", "params": {"result": row}}
    await rt.feed.tick()  # learn followed wallets and initialize the cursor
    assert await rt.feed.handle_message(pushed) == 1
    await rt.feed.drain()
    assert await rt.feed.handle_message(pushed) == 0
    await rt.feed.drain()
    clock.advance(400)
    assert (
        await chain_trade(
            rt,
            world,
            clock,
            WALLET_A,
            ago_s=460,
            size="50",
            tx="0x00",
        )
        == 1
    )
    sigs = await signals(rt)
    assert len(sigs) == 2
    assert sigs[-1].status == SignalStatus.REJECTED
    assert sigs[-1].reasons["items"][-1]["code"] == "STALE_SIGNAL"
    async with rt.sm() as s:
        assert await s.scalar(select(func.count()).select_from(SourceEvent)) == 2


async def test_backfill_is_history_only(env, world):
    rt, _clock, _ = env
    old = activity(WALLET_A, ts=NOW - timedelta(days=1), tx="0xold")
    future = activity(WALLET_A, ts=NOW + timedelta(seconds=1), tx="0xfuture")
    world.activity[WALLET_A] = [old, future]
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    assert await rt.observer.import_history(w) == 1
    assert await signals(rt) == []
    async with rt.sm() as s:
        rows = list((await s.scalars(select(SourceEvent))).all())
    assert len(rows) == 1 and rows[0].is_backfill is True and rows[0].tx_hash == "0xold"


async def test_restart_recovers_missed_trades_once(world, sm):
    clock = ManualClock(NOW)
    rest = FakeRest(clock)
    rest.set(YES, asks=[("0.40", "1000")])
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        rt = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        await rt.settings.update({"paper": {"execution_delay_ms": 0}})
        await add_and_snapshot(rt, WALLET_A, world=world)
        clock.advance(5)
        assert await chain_trade(rt, world, clock, WALLET_A, tx="0x1") == 1
        await rt.stop()
        # Downtime: two blocks arrive while no process is reading the persisted cursor.
        clock.advance(60)
        for i, ago in ((2, 30), (3, 10)):
            world.head += 1
            world.block_ts[world.head] = int(
                (clock.now() - timedelta(seconds=ago)).timestamp()
            )
            world.fill_logs.append(
                fill_log(WALLET_A, block=world.head, tx=f"0x{i}", log_index=i)
            )
        rt2 = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        await rt2.recover()
        assert await rt2.feed.tick() == 2
        await rt2.feed.drain()
        assert await rt2.feed.tick() == 0
        await rt2.feed.drain()
        sigs = await signals(rt2)
        assert len(sigs) == 3
        assert all(sg.status == SignalStatus.EXECUTED for sg in sigs)
        assert await rt2.ledger.verify(rt2.account_id) == []
        await rt2.stop()


async def test_source_sell_larger_than_copied_inventory(env, world):
    rt, clock, _rest = env
    await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(5)
    assert await chain_trade(rt, world, clock, WALLET_A, tx="0xb") == 1
    clock.advance(10)
    world.positions[WALLET_A] = []  # source fully exited
    assert (
        await chain_trade(
            rt, world, clock, WALLET_A, side="SELL", size="5000", tx="0xs"
        )
        == 1
    )
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
    assert await chain_trade(rt, world, clock, WALLET_A, tx="0xu") == 1
    [sig] = await signals(rt)
    assert sig.status == SignalStatus.SKIPPED
    assert sig.reasons["items"][-1]["code"] == "SOURCE_EQUITY_UNKNOWN"
    assert sig.source_equity_coverage == "UNAVAILABLE"


async def burst(rt, clock, world) -> None:
    """Both wallets each fire four $100 copies in one block."""
    await add_and_snapshot(rt, WALLET_A, world=world)
    await add_and_snapshot(rt, WALLET_B, world=world)
    clock.advance(5)
    world.head += 1
    world.block_ts[world.head] = int(clock.now().timestamp())
    for wallet_index, addr in enumerate((WALLET_A, WALLET_B)):
        for i in range(4):
            world.fill_logs.append(
                fill_log(
                    addr,
                    block=world.head,
                    tx=f"0x{wallet_index}{i}",
                    log_index=wallet_index * 4 + i,
                    size=str(100 + i),
                )
            )
    assert await rt.feed.tick() == 8
    await rt.feed.drain()


async def test_each_wallet_copies_within_its_half_of_the_bankroll(env, world):
    rt, clock, _ = env
    await rt.settings.update(
        {
            "sizing": {"mode": "FIXED_AMOUNT", "fixed_amount_usd": "100"},
            "risk": {"min_cash_reserve_usd": "0"},
        }
    )
    await burst(rt, clock, world)
    async with rt.sm() as s:
        rows = (
            await s.execute(
                select(SourceEvent.wallet_id, func.sum(CopySignal.filled_cost))
                .join(SourceEvent, SourceEvent.id == CopySignal.source_event_id)
                .where(CopySignal.status.in_([SignalStatus.EXECUTED, SignalStatus.PARTIAL]))
                .group_by(SourceEvent.wallet_id)
            )
        ).all()
    assert len(rows) == 2
    for _, cost in rows:
        assert D("200") < cost <= D("250"), "each wallet fills its $250 slice, never more"
    sigs = await signals(rt)
    assert any(r["code"] == "WALLET_SLICE" for sg in sigs for r in sg.reasons["items"])
    assert await rt.ledger.verify(rt.account_id) == []


async def test_simultaneous_trades_never_overspend(env, world):
    rt, clock, _ = env
    await rt.settings.update(
        {
            "sizing": {
                "mode": "FIXED_AMOUNT",
                "fixed_amount_usd": "100",
                "split_across_wallets": False,
            },
            "risk": {
                "max_pct_per_trade": "1",
                "max_position_usd": "10000",
                "max_market_exposure_usd": "10000",
                "max_total_exposure_pct": "1",
                "min_cash_reserve_usd": "0",
            },
        }
    )
    await burst(rt, clock, world)
    sigs = await signals(rt)
    executed = [sg for sg in sigs if sg.status == SignalStatus.EXECUTED]
    rejected = [sg for sg in sigs if sg.status == SignalStatus.REJECTED]
    assert len(sigs) == 8
    assert sum((sg.filled_cost for sg in executed), D(0)) <= D("500")
    assert len(executed) <= 5 and rejected
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash >= 0 and acct.reserved == 0
    assert await rt.ledger.verify(rt.account_id) == []


async def test_rpc_outage_does_not_lose_or_duplicate_trade(env, world):
    from app.integrations.http import UpstreamError

    rt, clock, _ = env
    await add_and_snapshot(rt, WALLET_A, world=world)
    clock.advance(5)
    world.head += 1
    world.block_ts[world.head] = int(clock.now().timestamp())
    world.fill_logs.append(fill_log(WALLET_A, block=world.head, tx="0xr"))
    world.rpc_errors = 10
    with pytest.raises(UpstreamError):
        await rt.feed.tick()
    assert await signals(rt) == []
    world.rpc_errors = 0
    assert await rt.feed.tick() == 1
    await rt.feed.drain()
    assert await rt.feed.tick() == 0
    await rt.feed.drain()
    assert len(await signals(rt)) == 1


async def test_paused_and_unfollowed_are_respected(env, world):
    rt, clock, _ = env
    w = await add_and_snapshot(rt, WALLET_A, world=world)
    await rt.settings.update({"strategies_paused": True})
    clock.advance(5)
    assert await chain_trade(rt, world, clock, WALLET_A, tx="0xp") == 1
    assert (await signals(rt))[-1].reasons["items"][-1]["code"] == "TRADING_HALTED"
    await rt.settings.update({"strategies_paused": False})
    await rt.registry.update(w.id, {"follow": False})
    clock.advance(5)
    before = len(await signals(rt))
    assert await chain_trade(rt, world, clock, WALLET_A, tx="0xq") == 1
    assert len(await signals(rt)) == before + 1
    assert (await signals(rt))[-1].reasons["items"][-1]["code"] == "NOT_FOLLOWED"


async def test_copies_below_the_minimum_carry_until_they_add_up_to_one_order(env, world):
    rt, clock, _ = env
    await rt.settings.update({"sizing": {"mode": "FIXED_AMOUNT", "fixed_amount_usd": "0.40"}})
    await add_and_snapshot(rt, WALLET_A, world=world)
    for i in range(3):
        clock.advance(30)
        await chain_trade(
            rt, world, clock, WALLET_A, ago_s=2, size="10", price="0.40", tx=f"0xb{i}"
        )
    sigs = await signals(rt)
    assert [s.status for s in sigs] == [
        SignalStatus.SKIPPED,
        SignalStatus.SKIPPED,
        SignalStatus.EXECUTED,
    ], [s.reasons for s in sigs]
    assert [s.reasons["items"][-1]["code"] for s in sigs[:2]] == ["BATCHED", "BATCHED"]
    placed = sigs[2]
    assert placed.filled_cost == D("1.20"), "one order for all three copies"
    assert any(r["code"] == "BATCH_PLACED" for r in placed.reasons["items"])
    clock.advance(30)
    await chain_trade(rt, world, clock, WALLET_A, ago_s=2, size="10", price="0.40", tx="0xb3")
    clock.advance(30)
    await chain_trade(
        rt, world, clock, WALLET_A, ago_s=2, side="SELL", size="10", price="0.40", tx="0xb4"
    )
    clock.advance(30)
    await chain_trade(rt, world, clock, WALLET_A, ago_s=2, size="10", price="0.40", tx="0xb5")
    last = (await signals(rt))[-1]
    assert last.status == SignalStatus.SKIPPED, "the sell ended the position, so no carry"
    assert "$0.40 now pending" in last.reasons["items"][-1]["message"]
    assert await rt.ledger.verify(rt.account_id) == []
