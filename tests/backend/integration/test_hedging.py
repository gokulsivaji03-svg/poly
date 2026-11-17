"""Copy + hedge coordination on one ledger, against the deterministic mocked world."""

import asyncio
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.db.models import (
    AccountMode,
    CopySignal,
    Fill,
    HedgeEvent,
    HedgePosition,
    LedgerEntry,
    Lot,
    LotClosure,
    Order,
    SignalStatus,
)
from app.domain.hedge_state import HedgeState
from app.services.ledger import InsufficientInventory, LedgerError
from scenario import COND, NO, WALLET_A, YES, FakeRest, World, chain_trade, make_runtime
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
                "hedge": {"enabled": True},
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
    assert await chain_trade(rt, world, clock, wallet, tx="0xbuy") == 1
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
    view = await rt.portfolio.view()
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
    view = await rt.portfolio.view()
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
    await copy_yes(rt, clock, world)
    clock.advance(6)
    await rt.hedger.execute(KEY)
    world.positions[WALLET_A] = []
    clock.advance(5)
    assert (
        await chain_trade(
            rt, world, clock, WALLET_A, side="SELL", size="500", tx="0xsell"
        )
        == 1
    )
    async with rt.sm() as s:
        sell = await s.scalar(select(CopySignal).where(CopySignal.side == "SELL"))
    assert sell.status == SignalStatus.SKIPPED
    assert "MATCHED_PAIRS_PRESERVED" in {r["code"] for r in sell.reasons["items"]}
    view = await rt.portfolio.view()
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


async def profitable_pairs(rt, clock, rest, world):
    await rt.settings.update({"copy": {"hedged_sell_policy": "UNWIND_IF_BETTER"}})
    rest.set(NO, asks=[("0.55", "1000")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    assert (await rt.hedger.execute(KEY))["status"] == "FILLED"
    rest.set(YES, asks=[("0.70", "1000")], bids=[("0.62", "1000")])
    rest.set(NO, asks=[("0.60", "1000")], bids=[("0.45", "1000")])
    clock.advance(6)


async def test_unwind_books_both_reviewed_fills_without_sequential_execution(env, world):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    # The second fetch would see no liquidity. A paper pair uses each reviewed book once,
    # rather than placing two independent orders which can leave one leg unsold.
    for token, bid in ((YES, "0.62"), (NO, "0.45")):
        rest.script[token] = [([], [(bid, "1000")]), ([], [])]
    assert await rt.hedger.review_unwind(COND) == "UNWOUND"
    assert await rt.ledger.open_lots(rt.account_id) == []
    async with rt.sm() as s:
        orders = (await s.scalars(select(Order).where(Order.kind == "PAIR_UNWIND"))).all()
        fills = (await s.scalars(select(Fill).join(Order).where(Order.kind == "PAIR_UNWIND"))).all()
    assert len(orders) == len(fills) == 2
    assert {o.status for o in orders} == {"FILLED"}
    assert {f.gross_shares for f in fills} == {D("100")}
    assert await rt.ledger.verify(rt.account_id) == []


async def test_unwind_rolls_back_both_legs_when_second_booking_fails(env, world, monkeypatch):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    before = await rt.ledger.get_account(rt.account_id)
    original = rt.ledger.book_sell
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            raise InsufficientInventory("second leg changed")
        return result

    monkeypatch.setattr(rt.ledger, "book_sell", fail_second)
    assert await rt.hedger.review_unwind(COND) == "INVENTORY_CHANGED"
    assert calls == 2
    lots = await rt.ledger.open_lots(rt.account_id)
    assert len(lots) == 2
    assert all(lt.shares_remaining == lt.matched_shares == D("100") for lt in lots)
    after = await rt.ledger.get_account(rt.account_id)
    assert (after.cash, after.realized_pnl, after.fees_paid) == (
        before.cash,
        before.realized_pnl,
        before.fees_paid,
    )
    async with rt.sm() as s:
        assert (await s.scalars(select(Order).where(Order.kind == "PAIR_UNWIND"))).all() == []
        assert (await s.scalars(select(LotClosure))).all() == []
    assert await rt.ledger.verify(rt.account_id) == []


async def test_unwind_consumes_only_matched_shares_from_partial_hedge(env, world):
    rt, clock, rest = env
    await rt.settings.update({"copy": {"hedged_sell_policy": "UNWIND_IF_BETTER"}})
    rest.set(NO, asks=[("0.55", "40")])
    await copy_yes(rt, clock, world)
    clock.advance(6)
    assert D((await rt.hedger.execute(KEY))["matched_shares"]) == D("40")
    rest.set(YES, asks=[("0.70", "1000")], bids=[("0.62", "1000")])
    rest.set(NO, asks=[("0.60", "1000")], bids=[("0.45", "1000")])
    clock.advance(6)
    assert await rt.hedger.review_unwind(COND) == "UNWOUND"
    [lot] = await rt.ledger.open_lots(rt.account_id)
    assert lot.token_id == YES
    assert lot.shares_remaining == D("60")
    assert lot.matched_shares == 0
    assert await rt.ledger.verify(rt.account_id) == []


@pytest.mark.parametrize("halt_flag", ["strategies_paused", "emergency_stop"])
@pytest.mark.parametrize("operation", ["merge", "unwind"])
async def test_halt_blocks_pair_disposal(env, world, halt_flag, operation):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    before = await rt.ledger.get_account(rt.account_id)
    await rt.settings.update({halt_flag: True})
    if operation == "merge":
        assert await rt.hedger.merge(KEY) == {"proceeds": "0", "reason": "TRADING_HALTED"}
    else:
        assert await rt.hedger.review_unwind(COND) == "TRADING_HALTED"
    assert (await rt.ledger.get_account(rt.account_id)).cash == before.cash
    lots = await rt.ledger.open_lots(rt.account_id)
    assert len(lots) == 2
    assert all(lt.matched_shares == D("100") for lt in lots)
    assert await rt.ledger.verify(rt.account_id) == []


@pytest.mark.parametrize("halt_flag", ["strategies_paused", "emergency_stop"])
async def test_halt_during_unwind_quote_prevents_any_fill(env, world, monkeypatch, halt_flag):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    original = rt.market_data.get_book
    calls = 0

    async def halt_after_book(*args, **kwargs):
        nonlocal calls
        book = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            await rt.settings.update({halt_flag: True})
        return book

    monkeypatch.setattr(rt.market_data, "get_book", halt_after_book)
    assert await rt.hedger.review_unwind(COND) == "TRADING_HALTED"
    assert calls == 2
    async with rt.sm() as s:
        assert (await s.scalars(select(Order).where(Order.kind == "PAIR_UNWIND"))).all() == []
    assert await rt.ledger.verify(rt.account_id) == []


async def test_unwind_applies_paper_slippage_before_comparing_payout(env, world):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    await rt.settings.update({"paper": {"extra_slippage_bps": 1000}})
    assert await rt.hedger.review_unwind(COND) == "HOLD_BETTER"
    assert len(await rt.ledger.open_lots(rt.account_id)) == 2
    assert await rt.ledger.verify(rt.account_id) == []


async def test_reset_removes_hedge_history_only_for_reset_account(env, world):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    assert (await rt.hedger.merge(KEY))["proceeds"] == "100"
    other_id = await rt.ledger.ensure_account(AccountMode.PAPER, D("200"), "other")
    async with rt.sm() as s, s.begin():
        other = HedgePosition(
            account_id=other_id,
            condition_id=COND,
            primary_token_id=YES,
            hedge_token_id=NO,
            state=HedgeState.UNHEDGED,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
        s.add(other)
        await s.flush()
        other_position_id = other.id
        s.add(
            HedgeEvent(
                hedge_position_id=other.id,
                kind="POSITION_OPENED",
                from_state=None,
                to_state=HedgeState.UNHEDGED,
                detail={},
                created_at=clock.now(),
            )
        )
    await rt.ledger.reset_account(rt.account_id, D("1234"))
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash == acct.starting_bankroll == D("1234")
    assert acct.realized_pnl == acct.fees_paid == acct.reserved == 0
    async with rt.sm() as s:
        assert (await s.scalars(select(HedgePosition.id))).all() == [other_position_id]
        assert (await s.scalars(select(HedgeEvent.hedge_position_id))).all() == [other_position_id]
        signals = (await s.scalars(select(CopySignal))).all()
        assert signals
        assert all(signal.order_id is None for signal in signals)
        assert {signal.status for signal in signals} == {SignalStatus.EXECUTED}
        for model in (Lot, Fill, Order):
            rows = await s.scalars(select(model).where(model.account_id == rt.account_id))
            assert rows.all() == []
        assert (await s.scalars(select(LotClosure))).all() == []
        [entry] = (
            await s.scalars(select(LedgerEntry).where(LedgerEntry.account_id == rt.account_id))
        ).all()
        assert entry.kind == "DEPOSIT" and entry.amount == D("1234")
    assert await rt.hedger.events_list(20) == []
    assert await rt.ledger.verify(rt.account_id) == []
    assert await rt.ledger.verify(other_id) == []


async def test_simulated_pair_disposal_rejects_live_accounts(env, world):
    rt, _clock, _rest = env
    live_id = await rt.ledger.ensure_account(AccountMode.LIVE, D("0"))
    with pytest.raises(LedgerError, match="only simulated"):
        await rt.ledger.merge_matched(COND, live_id)


async def test_merge_rechecks_halt_after_waiting_for_condition_lock(env, world):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    lock = rt.hedger._locks[COND]
    async with lock:
        pending = asyncio.create_task(rt.hedger.merge(KEY))
        await rt.settings.update({"emergency_stop": True})
    assert await pending == {"proceeds": "0", "reason": "TRADING_HALTED"}
    assert len(await rt.ledger.open_lots(rt.account_id)) == 2
    assert await rt.ledger.verify(rt.account_id) == []


async def test_unwind_refuses_whole_pair_when_second_leg_has_insufficient_depth(env, world):
    rt, clock, rest = env
    await profitable_pairs(rt, clock, rest, world)
    rest.set(NO, asks=[("0.60", "1000")], bids=[("0.45", "40")])
    assert await rt.hedger.review_unwind(COND) == "INSUFFICIENT_DEPTH"
    async with rt.sm() as s:
        assert (await s.scalars(select(Order).where(Order.kind == "PAIR_UNWIND"))).all() == []
    assert len(await rt.ledger.open_lots(rt.account_id)) == 2
    assert await rt.ledger.verify(rt.account_id) == []
