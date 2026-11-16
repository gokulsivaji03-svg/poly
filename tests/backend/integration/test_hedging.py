"""Copy + hedge coordination on one ledger, against the deterministic mocked world."""

import asyncio
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.db.models import CopySignal, HedgePosition, Order, SignalStatus
from app.domain.hedge_state import HedgeState
from scenario import COND, NO, WALLET_A, YES, FakeRest, World, activity, make_runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW

D = Decimal
KEY = f"{COND}:{YES}"


@pytest.fixture
def world():
    return World()


@pytest.fixture
async def env(sm, world):
    clock = ManualClock(NOW)
    rest = FakeRest(clock)
    rest.set(YES, asks=[("0.40", "1000")], bids=[("0.39", "1000")])
    rest.set(NO, asks=[("0.62", "1000")], bids=[("0.58", "1000")])
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        rt = await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))
        await rt.settings.update(
            {
                "paper": {"execution_delay_ms": 0},
                "sizing": {"mode": "FIXED_AMOUNT", "fixed_amount_usd": "40"},
                "risk": {"min_cash_reserve_usd": "0"},
            }
        )
        yield rt, clock, rest
        await rt.stop()


async def copy_yes(rt, clock, world, *, wallet=WALLET_A):
    """Source buys YES; we copy $40 at 0.40 => 100 YES."""
    world.value[wallet] = "5000"
    world.cash[wallet] = 5000
    w = await rt.registry.add(wallet)
    await rt.estimator.snapshot(w)
    clock.advance(5)
    world.activity[wallet] = [activity(wallet, ts=clock.now(), tx="0xbuy")]
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    return w


async def hedge_state(rt):
    async with rt.sm() as s:
        pos = await s.scalar(select(HedgePosition))
        return HedgeState(pos.state) if pos else None


async def test_alert_only_then_manual_hedge_locks_profit(env, world):
    rt, clock, rest = env
    await copy_yes(rt, clock, world)
    [opp] = await rt.hedger.scan()
    assert not opp.quote.viable  # NO at 0.62: 1 - 0.40 - 0.62 < 0
    assert await hedge_state(rt) is HedgeState.UNHEDGED
    rest.set(NO, asks=[("0.55", "1000")], bids=[("0.53", "1000")])
    clock.advance(6)  # let the cached book expire
    [opp] = await rt.hedger.scan()
    assert opp.quote.viable and opp.quote.matched_shares == D("100")
    assert opp.quote.net_profit == D("5")
    assert await hedge_state(rt) is HedgeState.HEDGE_OPPORTUNITY
    assert len(await rt.ledger.open_lots(rt.account_id)) == 1  # alert-only: nothing bought
    res = await rt.hedger.execute(KEY)
    assert res["status"] == "FILLED", res
    assert await hedge_state(rt) is HedgeState.FULLY_HEDGED
    view = await rt.copy.portfolio()
    assert view.locked_profit == D("5")
    assert view.cash == D("1000") - D("40") - D("55")
    assert await rt.ledger.verify(rt.account_id) == []
    payload = await rt.hedger.payload()
    assert payload["opportunities"][0]["state"] == "FULLY_HEDGED"


async def test_auto_paper_hedges_and_never_rescans_hedged_quantity(env, world):
    rt, clock, rest = env
    await rt.settings.update({"hedge": {"mode": "AUTO_PAPER"}})
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.scan()
    assert await hedge_state(rt) is HedgeState.FULLY_HEDGED
    clock.advance(6)
    [opp] = await rt.hedger.scan()
    assert opp.quote.reasons == ("NOTHING_TO_HEDGE",)
    async with rt.sm() as s:
        hedges = (await s.scalars(select(Order).where(Order.kind == "HEDGE_BUY"))).all()
    assert len(hedges) == 1


async def test_liquidity_vanishing_before_execution_gives_partial_hedge(env, world):
    rt, clock, rest = env
    await rt.settings.update({"paper": {"execution_delay_ms": 3000}})
    await copy_yes(rt, clock, world)
    rest.set(NO, asks=[("0.55", "1000")])
    clock.advance(6)
    # Quote sees deep liquidity; by execution time only 40 shares remain within the limit.
    rest.script[NO] = [([("0.55", "1000")], []), ([("0.55", "40"), ("0.70", "1000")], [])]
    res = await rt.hedger.execute(KEY)
    assert res["status"] == "PARTIALLY_FILLED", res
    assert D(res["matched_shares"]) == D("40")
    assert await hedge_state(rt) is HedgeState.PARTIALLY_HEDGED
    lots = await rt.ledger.open_lots(rt.account_id)
    yes = next(lt for lt in lots if lt.token_id == YES)
    assert yes.matched_shares == D("40")
    view = await rt.copy.portfolio()
    assert view.locked_profit == D("40") - D("16") - D("22")
    assert (await rt.ledger.get_account(rt.account_id)).reserved == 0
    assert await rt.ledger.verify(rt.account_id) == []


async def test_concurrent_hedge_requests_place_one_order(env, world):
    rt, clock, rest = env
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    results = await asyncio.gather(*[rt.hedger.execute(KEY) for _ in range(5)])
    assert sum(1 for r in results if r["status"] == "FILLED") == 1
    async with rt.sm() as s:
        hedges = (await s.scalars(select(Order).where(Order.kind == "HEDGE_BUY"))).all()
    assert len(hedges) == 1
    assert await rt.ledger.verify(rt.account_id) == []


async def test_source_sell_after_full_hedge_preserves_pairs(env, world):
    rt, clock, rest = env
    rest.set(NO, asks=[("0.55", "1000")])
    w = await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.execute(KEY)
    world.positions[WALLET_A] = []
    clock.advance(5)
    world.activity[WALLET_A].append(
        activity(WALLET_A, ts=clock.now(), side="SELL", size="500", tx="0xsell")
    )
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    async with rt.sm() as s:
        sell = await s.scalar(select(CopySignal).where(CopySignal.side == "SELL"))
    assert sell.status == SignalStatus.SKIPPED
    assert "MATCHED_PAIRS_PRESERVED" in {r["code"] for r in sell.reasons["items"]}
    view = await rt.copy.portfolio()
    assert view.locked_profit == D("5")
    assert await hedge_state(rt) is HedgeState.FULLY_HEDGED


async def test_unwind_policy_holds_when_bids_do_not_beat_locked_payout(env, world):
    rt, clock, rest = env
    await rt.settings.update({"copy": {"hedged_sell_policy": "UNWIND_IF_BETTER"}})
    rest.set(NO, asks=[("0.55", "1000")], bids=[("0.53", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.execute(KEY)
    assert await rt.hedger.review_unwind(COND) == "HOLD_BETTER"
    # A dislocation where both bids sum above $1 makes unwinding strictly better.
    rest.set(YES, asks=[("0.70", "1000")], bids=[("0.62", "1000")])
    rest.set(NO, asks=[("0.60", "1000")], bids=[("0.45", "1000")])
    clock.advance(6)
    assert await rt.hedger.review_unwind(COND) == "UNWOUND"
    assert await rt.ledger.open_lots(rt.account_id) == []
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.realized_pnl == D("100") * (D("0.62") + D("0.45")) - D("95")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_fees_make_hedge_unviable_end_to_end(env, world):
    rt, clock, rest = env
    world.fee_rate = "0.07"
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    [opp] = await rt.hedger.scan()
    assert not opp.quote.viable
    assert opp.quote.reasons == ("NO_PROFITABLE_DEPTH",)


async def test_missing_book_and_unverified_mapping(env, world):
    rt, clock, rest = env
    await copy_yes(rt, clock, world)
    rest.books.pop(NO)
    rt.market_data._books.pop(NO, None)
    clock.advance(6)
    [opp] = await rt.hedger.scan()
    assert opp.quote.reasons == ("NO_BOOK",)


async def test_settlement_books_observed_payouts(env, world):
    rt, clock, rest = env
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.execute(KEY)
    assert await rt.hedger.settle_resolved() == 0  # unresolved: nothing booked
    world.resolution = {YES: "0", NO: "1"}
    assert await rt.hedger.settle_resolved() == 1
    assert await hedge_state(rt) is HedgeState.SETTLED
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash == D("1000") - D("95") + D("100")
    assert acct.realized_pnl == D("5")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_restart_with_pending_hedge_recovers(env, world):
    rt, clock, rest = env
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.scan()
    async with rt.sm() as s, s.begin():
        pos = await s.scalar(select(HedgePosition))
        pos.state = HedgeState.HEDGE_PENDING
    assert await rt.hedger.recover() == 1
    assert await hedge_state(rt) is HedgeState.HEDGE_FAILED
    clock.advance(6)
    await rt.hedger.scan()
    assert await hedge_state(rt) is HedgeState.HEDGE_OPPORTUNITY
    events = await rt.hedger.events_list(20)
    assert {"ORDER_FAILED", "OPPORTUNITY_FOUND"} <= {e["kind"] for e in events}


async def test_merge_simulation_realizes_locked_profit(env, world):
    rt, clock, rest = env
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.execute(KEY)
    out = await rt.hedger.merge(KEY)
    assert out["proceeds"] == "100"
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.realized_pnl == D("5")
    assert await rt.ledger.open_lots(rt.account_id) == []
