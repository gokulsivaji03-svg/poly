"""Copy-path latency benchmark: real PostgreSQL, in-process fakes for every network call.

    uv run pytest -m bench -s

Pushes a burst of fills through ``ChainFeed.handle_message`` as the WebSocket would and
prints per-stage percentiles from ``copy_signals.stage_us``. Network latency is excluded on
purpose: this measures the code and database between receipt and execution.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.core.stages import render, summarize
from app.db.models import CopySignal, SignalStatus
from scenario import NO, YES, FakeRest, World, fill_log, make_runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW

pytestmark = pytest.mark.bench

WALLETS = 4
FILLS_PER_WALLET = 50


def pushed(result):
    return {"jsonrpc": "2.0", "method": "eth_subscription", "params": {"result": result}}


@pytest.mark.parametrize("burst", [False, True], ids=["isolated", "burst"])
async def test_copy_path_latency(sm, burst):
    world = World()
    clock = ManualClock(NOW)
    rest = FakeRest(clock)
    rest.set(YES, asks=[("0.40", "10000000")], bids=[("0.39", "10000000")])
    rest.set(NO, asks=[("0.62", "10000000")], bids=[("0.60", "10000000")])
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        rt = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        try:
            await rt.settings.update(
                {
                    "paper": {"starting_bankroll_usd": "1000000", "execution_delay_ms": 0},
                    "risk": {
                        "max_position_usd": "1000000",
                        "max_market_exposure_usd": "1000000",
                        "max_total_exposure_pct": "1",
                        "max_buys_per_market": 1000000,
                    },
                }
            )
            await rt.ledger.reset_account(rt.account_id, Decimal("1000000"))
            addresses = ["0x" + f"{i + 1:02x}" * 20 for i in range(WALLETS)]
            for address in addresses:
                world.value[address], world.cash[address] = "50000", 50000
                await rt.estimator.snapshot(await rt.registry.add(address))
            clock.advance(30)
            await rt.feed.tick()
            block = world.head + 1
            ts = int((clock.now() - timedelta(seconds=1)).timestamp())
            await rt.feed.handle_message(pushed({"number": hex(block), "timestamp": hex(ts)}))
            for i in range(FILLS_PER_WALLET):
                for w, address in enumerate(addresses):
                    log = fill_log(
                        address, block=block, size="50", tx=f"0x{w:02x}{i:04x}", log_index=i
                    )
                    await rt.feed.handle_message(pushed(log))
                    if not burst:
                        await rt.feed.drain()
            await rt.feed.drain()
            async with rt.sm() as s:
                rows = (await s.execute(select(CopySignal.status, CopySignal.stage_us))).all()
        finally:
            await rt.stop()
    assert len(rows) == WALLETS * FILLS_PER_WALLET
    assert {status for status, _ in rows} == {SignalStatus.EXECUTED}, "bench measures full path"
    mode = "all pushed at once" if burst else "each copied before the next arrives"
    print(f"\n{WALLETS} wallets x {FILLS_PER_WALLET} fills, {mode}; paper, no network\n")
    print(render(summarize(stage for _, stage in rows)))
