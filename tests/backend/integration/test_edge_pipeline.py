"""Severe end-to-end edge cases: settlement, new BUY rules, SELL x hedge, halts, cash floor."""

import asyncio
import contextlib
import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
import respx
from app.core.clock import ManualClock
from app.core.money import usdc_down
from app.db.models import (
    CashReservation,
    CopySignal,
    Fill,
    HedgePosition,
    LedgerEntry,
    Lot,
    LotClosure,
    LotOrigin,
    Order,
    SignalStatus,
    SystemEvent,
)
from app.domain.hedge_state import HedgeState
from scenario import COND, NO, WALLET_A, WALLET_B, YES, FakeRest, World, activity, make_runtime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW

D = Decimal
KEY = f"{COND}:{YES}"
WALLET_C = "0x" + "c3" * 20
WALLET_D = "0x" + "d4" * 20
_tx = itertools.count()


class EdgeWorld(World):
    """World whose token refs can report a resolution without a final price for some tokens."""

    def __init__(self) -> None:
        super().__init__()
        self.unknown_payout: set[str] = set()

    def _tokens(self, request):
        resp = super()._tokens(request)
        if not self.unknown_payout:
            return resp
        body = resp.json()
        for row in body["data"]:
            if row["token_id"] in self.unknown_payout:
                row.pop("final_price", None)
        return type(resp)(200, json=body)


@pytest.fixture
def world():
    return EdgeWorld()


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


async def follow(rt, world, wallet=WALLET_A):
    world.value[wallet] = "5000"
    world.cash[wallet] = 5000
    w = await rt.registry.add(wallet)
    await rt.estimator.snapshot(w)
    return w


async def trade(rt, clock, world, w, *, advance=5, **kw):
    """The source makes one trade; we poll and return the resulting (latest) signal."""
    clock.advance(advance)
    row = activity(w.address, ts=clock.now(), tx=f"0x{next(_tx):04x}", **kw)
    world.activity.setdefault(w.address, []).append(row)
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    return (await signals(rt))[-1]


async def signals(rt):
    async with rt.sm() as s:
        return list((await s.scalars(select(CopySignal).order_by(CopySignal.id))).all())


def codes(sig):
    return [r["code"] for r in sig.reasons["items"]]


async def hedge(rt, clock, rest, *, depth="1000"):
    rest.set(NO, asks=[("0.55", depth)], bids=[("0.53", "1000")])
    clock.advance(6)  # let the cached complement book expire
    res = await rt.hedger.execute(KEY)
    assert res["status"] in ("FILLED", "PARTIALLY_FILLED"), res
    return res


async def count(rt, model, *where):
    async with rt.sm() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


async def settlement_closures(rt):
    async with rt.sm() as s:
        rows = (
            await s.execute(
                select(Lot.token_id, LotClosure)
                .join(Lot, Lot.id == LotClosure.lot_id)
                .where(LotClosure.kind == "SETTLEMENT")
                .order_by(Lot.token_id)
            )
        ).all()
    return {token: c for token, c in rows}


async def hedge_state(rt):
    async with rt.sm() as s:
        pos = await s.scalar(select(HedgePosition))
        return HedgeState(pos.state) if pos else None


# --------------------------------------------------------------------------- settlement


async def test_fifty_fifty_resolution_on_fully_hedged_pair(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    assert (await trade(rt, clock, world, w)).status == SignalStatus.EXECUTED  # 100 YES @ .40
    await hedge(rt, clock, rest)  # 100 NO @ .55, matched
    world.resolution = {YES: "0.5", NO: "0.5"}
    assert await rt.hedger.settle_resolved() == 1
    acct = await rt.ledger.get_account(rt.account_id)
    # Each pair pays 0.5 + 0.5 = $1 regardless of the 50/50 split: 100 - 95 = +5.
    assert acct.cash == D("1005")
    assert acct.realized_pnl == D("5")
    closures = await settlement_closures(rt)
    assert (closures[YES].proceeds, closures[YES].realized_pnl) == (D("50"), D("10"))
    assert (closures[NO].proceeds, closures[NO].realized_pnl) == (D("50"), D("-5"))
    assert await rt.ledger.open_lots(rt.account_id) == []
    view = await rt.copy.portfolio()
    assert view.equity == view.cash == D("1005")
    assert view.locked_profit == 0 and view.positions == ()
    assert await hedge_state(rt) is HedgeState.SETTLED
    assert await rt.ledger.verify(rt.account_id) == []


async def test_fifty_fifty_resolution_on_partially_hedged_position(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES for $40
    res = await hedge(rt, clock, rest, depth="40")  # only 40 NO @ .55 = $22
    assert D(res["matched_shares"]) == D("40")
    world.resolution = {YES: "0.5", NO: "0.5"}
    assert await rt.hedger.settle_resolved() == 1
    acct = await rt.ledger.get_account(rt.account_id)
    # Payout: 100 * .5 + 40 * .5 = 70 against 62 cost.
    assert acct.cash == D("1000") - D("62") + D("70")
    assert acct.realized_pnl == D("8")
    assert await rt.ledger.open_lots(rt.account_id) == []
    assert await rt.ledger.verify(rt.account_id) == []


async def test_fifty_fifty_resolution_on_unhedged_lot_with_share_fees(env, world):
    """Fee-in-shares lot (fractional net shares) settling at 0.5 must round only once."""
    rt, clock, rest = env
    world.fee_rate = "0.07"
    rest.set(YES, asks=[("0.60", "1000")], bids=[("0.59", "1000")])
    w = await follow(rt, world)
    sig = await trade(rt, clock, world, w, price="0.60")
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    [lot] = await rt.ledger.open_lots(rt.account_id)
    assert lot.shares_acquired < sig.filled_cost / D("0.60")  # fee taken in shares
    cash_before = (await rt.ledger.get_account(rt.account_id)).cash
    world.resolution = {YES: "0.5", NO: "0.5"}
    assert await rt.hedger.settle_resolved() == 1
    expected = usdc_down(lot.shares_acquired * D("0.5"))
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash == cash_before + expected
    assert acct.realized_pnl == expected - lot.cost_total
    assert acct.realized_pnl < 0  # bought at .60, redeemed at .50
    assert acct.cash == D("1000") + acct.realized_pnl  # no other cash movements
    assert await rt.ledger.verify(rt.account_id) == []


async def test_closed_market_settles_and_then_refuses_new_copy_buys(env, world):
    rt, clock, _rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES for $40 (market cached as open)
    world.resolution = {YES: "0", NO: "1"}  # Gamma now lists it only under closed=true
    assert await rt.hedger.settle_resolved() == 1
    assert await rt.ledger.open_lots(rt.account_id) == []
    acct = await rt.ledger.get_account(rt.account_id)
    assert (acct.cash, acct.realized_pnl) == (D("960"), D("-40"))
    # A late source buy in the resolved market must not reserve or trade.
    sig = await trade(rt, clock, world, w)
    assert sig.status == SignalStatus.REJECTED and codes(sig)[-1] == "MARKET_CLOSED"
    assert await count(rt, CashReservation) == 1  # only the original buy's reservation
    assert await count(rt, Order) == 1
    assert (await rt.ledger.get_account(rt.account_id)).cash == D("960")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_settlement_is_idempotent_sequential_and_concurrent(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)
    await hedge(rt, clock, rest)
    world.resolution = {YES: "1", NO: "0"}
    await asyncio.gather(rt.hedger.settle_resolved(), rt.hedger.settle_resolved())
    assert await rt.hedger.settle_resolved() == 0  # nothing open any more
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash == D("1000") - D("95") + D("100")
    assert acct.realized_pnl == D("5")
    assert await count(rt, LedgerEntry, LedgerEntry.kind == "SETTLEMENT") == 1
    assert await count(rt, LotClosure, LotClosure.kind == "SETTLEMENT") == 2
    assert await rt.ledger.verify(rt.account_id) == []


async def test_resolution_with_unobservable_payout_is_not_settled_as_zero(env, world):
    rt, clock, _rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)
    world.resolution = {YES: "1", NO: "0"}
    world.unknown_payout = {YES}  # resolved flag visible, our token's final price not yet
    assert await rt.hedger.settle_resolved() == 0
    [lot] = await rt.ledger.open_lots(rt.account_id)
    assert lot.shares_remaining == D("100")
    assert (await rt.ledger.get_account(rt.account_id)).cash == D("960")
    assert await count(rt, SystemEvent, SystemEvent.kind == "RESOLUTION_PAYOUT_UNKNOWN") == 1
    world.unknown_payout = set()
    assert await rt.hedger.settle_resolved() == 1
    acct = await rt.ledger.get_account(rt.account_id)
    assert (acct.cash, acct.realized_pnl) == (D("1060"), D("60"))
    assert await rt.ledger.verify(rt.account_id) == []


async def test_penny_entry_redeemed_at_one_is_exact(env, world):
    rt, clock, rest = env
    await rt.settings.update({"risk": {"max_position_usd": "1000"}})
    rest.set(YES, asks=[("0.001", "100000")], bids=[])
    w = await follow(rt, world)
    sig = await trade(rt, clock, world, w, price="0.001", size="100000")
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    assert (sig.filled_shares, sig.filled_cost) == (D("40000"), D("40"))
    world.resolution = {YES: "1", NO: "0"}
    assert await rt.hedger.settle_resolved() == 1
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.cash == D("1000") - D("40") + D("40000")
    assert acct.realized_pnl == D("39960")
    assert await rt.ledger.verify(rt.account_id) == []


# ---------------------------------------------------------------------- new BUY rules


@pytest.mark.parametrize(
    ("price", "ask", "status", "code"),
    [
        ("0.90", "0.90", SignalStatus.EXECUTED, None),  # boundary is inclusive
        ("0.91", "0.90", SignalStatus.REJECTED, "ENTRY_PRICE_TOO_HIGH"),
        ("0.999", "0.90", SignalStatus.REJECTED, "ENTRY_PRICE_TOO_HIGH"),
    ],
)
async def test_max_entry_price_boundary(env, world, price, ask, status, code):
    rt, clock, rest = env
    rest.set(YES, asks=[(ask, "1000")], bids=[("0.89", "1000")])
    w = await follow(rt, world)
    sig = await trade(rt, clock, world, w, price=price)
    assert sig.status == status, sig.reasons
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.reserved == 0
    if code is None:
        assert sig.avg_fill_price == D("0.90")
        assert acct.cash == D("1000") - sig.filled_cost
    else:
        assert codes(sig)[-1] == code
        assert await count(rt, CashReservation) == 0
        assert await count(rt, Order) == 0
        assert acct.cash == D("1000")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_max_buys_per_market_counts_condition_and_restarts_after_reset(env, world):
    rt, clock, rest = env
    rest.set(NO, asks=[("0.55", "1000")], bids=[("0.53", "1000")])
    await rt.settings.update({"risk": {"max_buys_per_market": 2}})
    w = await follow(rt, world)
    assert (await trade(rt, clock, world, w)).status == SignalStatus.EXECUTED
    assert (await trade(rt, clock, world, w)).status == SignalStatus.EXECUTED
    third = await trade(rt, clock, world, w)
    assert third.status == SignalStatus.REJECTED and codes(third)[-1] == "MAX_BUYS_PER_MARKET"
    # The cap is per condition: the complementary outcome is blocked too.
    other = await trade(rt, clock, world, w, token=NO, price="0.55")
    assert other.status == SignalStatus.REJECTED and codes(other)[-1] == "MAX_BUYS_PER_MARKET"
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.reserved == 0 and acct.cash == D("920")
    assert await count(rt, CashReservation) == 2
    await rt.ledger.reset_account(rt.account_id, D("500"))
    after = await trade(rt, clock, world, w)
    assert after.status == SignalStatus.EXECUTED, after.reasons
    # $40 target capped at 5% of the fresh $500 equity.
    assert (await rt.ledger.get_account(rt.account_id)).cash == D("475")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_hedge_buys_do_not_consume_copy_buy_allowance(env, world):
    rt, clock, rest = env
    await rt.settings.update({"risk": {"max_buys_per_market": 2}})
    w = await follow(rt, world)
    await trade(rt, clock, world, w)
    await hedge(rt, clock, rest)
    second = await trade(rt, clock, world, w)
    assert second.status == SignalStatus.EXECUTED, second.reasons
    third = await trade(rt, clock, world, w)
    assert codes(third)[-1] == "MAX_BUYS_PER_MARKET"
    assert await rt.ledger.verify(rt.account_id) == []


async def test_max_buys_per_market_holds_across_concurrent_wallets(env, world, monkeypatch):
    rt, clock, _rest = env
    await rt.settings.update({"risk": {"max_buys_per_market": 1}})
    wa = await follow(rt, world, WALLET_A)
    wb = await follow(rt, world, WALLET_B)
    # Two followed wallets buy the same market in the same block; the leader polls both
    # wallets concurrently (one task per wallet). Line both signals up at the reservation.
    # Barrier: on the racy code both signals reach reserve() together; with the per-market
    # lock the second never gets there, so give up waiting after a short timeout.
    arrived = 0
    both = asyncio.Event()
    original = rt.ledger.reserve

    async def reserve(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived >= 2:
            both.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both.wait(), timeout=1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(rt.ledger, "reserve", reserve)
    clock.advance(5)
    for i, wl in enumerate((wa, wb)):
        world.activity[wl.address] = [activity(wl.address, ts=clock.now(), tx=f"0xcc{i}")]
    await asyncio.gather(
        rt.monitor.poll_once(await rt.registry.get(wa.id)),
        rt.monitor.poll_once(await rt.registry.get(wb.id)),
    )
    sigs = await signals(rt)
    assert sorted(s.status for s in sigs) == [SignalStatus.EXECUTED, SignalStatus.REJECTED]
    [rejected] = [s for s in sigs if s.status == SignalStatus.REJECTED]
    assert "MAX_BUYS_PER_MARKET" in codes(rejected)


# ------------------------------------------------------------------- SELL x inventory


async def test_sell_before_any_copied_buy_books_nothing(env, world):
    rt, clock, _rest = env
    w = await follow(rt, world)
    sig = await trade(rt, clock, world, w, side="SELL", size="300")
    assert sig.status == SignalStatus.SKIPPED
    assert "NO_COPIED_INVENTORY" in codes(sig)
    assert await count(rt, Order) == 0 and await count(rt, Fill) == 0
    assert (await rt.ledger.get_account(rt.account_id)).cash == D("1000")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_sell_of_token_held_only_as_hedge_lot_is_ignored(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES copy
    await hedge(rt, clock, rest)  # 100 NO hedge lot
    sig = await trade(rt, clock, world, w, side="SELL", token=NO, size="500", price="0.53")
    assert sig.status == SignalStatus.SKIPPED
    assert "NO_COPIED_INVENTORY" in codes(sig)
    lots = await rt.ledger.open_lots(rt.account_id)
    assert {(lt.token_id, lt.shares_remaining, lt.matched_shares) for lt in lots} == {
        (YES, D("100"), D("100")),
        (NO, D("100"), D("100")),
    }
    assert await count(rt, Order, Order.kind == "COPY_SELL") == 0
    assert (await rt.copy.portfolio()).locked_profit == D("5")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_full_exit_of_copied_complement_spares_hedge_lot(env, world):
    """We hold NO both as a COPY lot and as a HEDGE lot; a source exit sells only the copy."""
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES copy
    await hedge(rt, clock, rest)  # 100 NO hedge, matched with YES
    buy_no = await trade(rt, clock, world, w, token=NO, price="0.55")
    assert buy_no.status == SignalStatus.EXECUTED, buy_no.reasons
    copied_no = buy_no.filled_shares
    world.positions[WALLET_A] = []  # source fully exited NO
    sig = await trade(rt, clock, world, w, side="SELL", token=NO, size="5000", price="0.53")
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    assert sig.filled_shares == copied_no
    lots = await rt.ledger.open_lots(rt.account_id)
    assert {(lt.token_id, lt.origin, lt.shares_remaining, lt.matched_shares) for lt in lots} == {
        (YES, LotOrigin.COPY, D("100"), D("100")),
        (NO, LotOrigin.HEDGE, D("100"), D("100")),
    }
    assert (await rt.copy.portfolio()).locked_profit == D("5")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_partial_hedge_full_source_exit_sells_only_unmatched(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES
    await hedge(rt, clock, rest, depth="40")  # 40 matched
    world.positions[WALLET_A] = []
    sig = await trade(rt, clock, world, w, side="SELL", size="5000", price="0.39")
    assert sig.status == SignalStatus.EXECUTED, sig.reasons
    assert sig.filled_shares == D("60")
    assert "MATCHED_PAIRS_PRESERVED" in codes(sig)
    yes = next(lt for lt in await rt.ledger.open_lots(rt.account_id) if lt.token_id == YES)
    assert (yes.shares_remaining, yes.matched_shares) == (D("40"), D("40"))
    acct = await rt.ledger.get_account(rt.account_id)
    # 60 sold at .39 against .40 cost.
    assert acct.realized_pnl == D("60") * (D("0.39") - D("0.40"))
    assert (await rt.copy.portfolio()).locked_profit == D("40") - D("16") - D("22")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_sell_from_one_wallet_never_touches_another_wallets_lots(env, world):
    rt, clock, _rest = env
    wa = await follow(rt, world, WALLET_A)
    wb = await follow(rt, world, WALLET_B)
    await trade(rt, clock, world, wa)
    await trade(rt, clock, world, wb)
    world.positions[WALLET_A] = []
    sig = await trade(rt, clock, world, wa, side="SELL", size="5000", price="0.39")
    assert sig.filled_shares == D("100")
    [lot] = await rt.ledger.open_lots(rt.account_id)
    assert lot.source_wallet == WALLET_B and lot.shares_remaining == D("100")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_sell_into_empty_bid_book_keeps_inventory(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)
    rest.set(YES, asks=[("0.40", "1000")], bids=[])
    clock.advance(6)
    world.positions[WALLET_A] = []
    sig = await trade(rt, clock, world, w, side="SELL", size="500", price="0.39")
    assert sig.status == SignalStatus.REJECTED, sig.reasons
    [lot] = await rt.ledger.open_lots(rt.account_id)
    assert lot.shares_remaining == D("100")
    acct = await rt.ledger.get_account(rt.account_id)
    assert (acct.cash, acct.reserved, acct.realized_pnl) == (D("960"), 0, 0)
    assert await rt.ledger.verify(rt.account_id) == []


async def test_proportional_sells_respect_fraction_then_cap_at_unhedged(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES @ .40
    await hedge(rt, clock, rest, depth="60")  # 60 matched, 40 free
    pos = {"token_id": YES, "condition_id": COND, "current_size": 1000}
    world.positions[WALLET_A] = [pos]  # source sold 500 of 1500 => 1/3
    first = await trade(rt, clock, world, w, side="SELL", size="500", price="0.39")
    assert first.status == SignalStatus.EXECUTED, first.reasons
    assert first.filled_shares == D("33.33")  # floor(100/3) to the share step
    world.positions[WALLET_A] = [{**pos, "current_size": 500}]  # sold 500 of 1000 => 1/2
    second = await trade(rt, clock, world, w, side="SELL", size="500", price="0.39")
    assert "CAPPED_TO_UNHEDGED_INVENTORY" in codes(second)
    assert second.filled_shares == D("6.67")
    yes = next(lt for lt in await rt.ledger.open_lots(rt.account_id) if lt.token_id == YES)
    assert (yes.shares_remaining, yes.matched_shares) == (D("60"), D("60"))
    assert yes.cost_remaining == D("24")
    acct = await rt.ledger.get_account(rt.account_id)
    assert acct.realized_pnl == D("40") * (D("0.39") - D("0.40"))
    assert await rt.ledger.verify(rt.account_id) == []


async def test_buy_indexed_after_its_sell_does_not_leave_orphan_position(env, world):
    rt, clock, _rest = env
    w = await follow(rt, world)
    clock.advance(10)
    t_buy, t_sell = clock.now() - timedelta(seconds=4), clock.now() - timedelta(seconds=2)
    world.positions[WALLET_A] = []
    world.activity[WALLET_A] = [activity(WALLET_A, ts=t_sell, side="SELL", tx="0x5e11")]
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    [sell] = await signals(rt)
    assert "NO_COPIED_INVENTORY" in codes(sell)
    # The source's earlier BUY is indexed late and shows up in the overlap window.
    world.activity[WALLET_A].append(activity(WALLET_A, ts=t_buy, tx="0xb0b"))
    clock.advance(5)
    await rt.monitor.poll_once(await rt.registry.get(w.id))
    buy = (await signals(rt))[-1]
    assert buy.side == "BUY"
    # Either refuse the stale BUY or flatten it: we must not end up holding what the
    # source no longer holds (its exit was already consumed as NO_COPIED_INVENTORY).
    assert await rt.ledger.open_lots(rt.account_id) == [], buy.status
    assert buy.status == SignalStatus.SKIPPED and "SOURCE_SOLD_AFTER" in codes(buy)


# ----------------------------------------------------------------- halts and cash floor


async def test_emergency_stop_between_reservation_and_fill(env, world, monkeypatch):
    rt, clock, rest = env
    await rt.settings.update({"hedge": {"mode": "AUTO_PAPER"}})
    w = await follow(rt, world)
    original = rt.executor.execute

    async def stop_then_execute(req, **kwargs):
        # Operator hits emergency stop after the signal reserved but before the order exists.
        await rt.settings.update({"emergency_stop": True})
        assert await rt.ledger.release_idle(rt.account_id) == 1
        return await original(req, **kwargs)

    monkeypatch.setattr(rt.executor, "execute", stop_then_execute)
    sig = await trade(rt, clock, world, w)
    assert sig.status == SignalStatus.FAILED, sig.reasons
    acct = await rt.ledger.get_account(rt.account_id)
    assert (acct.cash, acct.reserved) == (D("1000"), 0)
    assert await rt.ledger.open_lots(rt.account_id) == []
    monkeypatch.setattr(rt.executor, "execute", original)
    # Nothing else may trade while stopped: buys, sells, hedges.
    nxt = await trade(rt, clock, world, w)
    assert nxt.status == SignalStatus.REJECTED and codes(nxt)[-1] == "TRADING_HALTED"
    sell = await trade(rt, clock, world, w, side="SELL", size="10")
    assert codes(sell)[-1] == "TRADING_HALTED"
    rest.set(NO, asks=[("0.55", "1000")])
    assert (await rt.hedger.execute(KEY))["status"] == "REJECTED"
    await rt.hedger.scan()
    assert await count(rt, Order, Order.status != "FAILED") == 0
    assert await count(rt, CashReservation, CashReservation.status == "OPEN") == 0
    assert await rt.ledger.verify(rt.account_id) == []


async def test_concurrent_signals_with_tiny_cash_never_breach_reserve_floor(env, world):
    rt, clock, _rest = env
    await rt.ledger.reset_account(rt.account_id, D("30"))
    await rt.settings.update(
        {
            "sizing": {"mode": "FIXED_AMOUNT", "fixed_amount_usd": "10"},
            "risk": {
                "min_cash_reserve_usd": "5",
                "max_pct_per_trade": "1",
                "max_total_exposure_pct": "1",
                "min_liquidity_usd": "0",
            },
        }
    )
    wallets = [await follow(rt, world, a) for a in (WALLET_A, WALLET_B, WALLET_C, WALLET_D)]
    clock.advance(5)
    for wl in wallets:
        world.activity[wl.address] = [
            activity(wl.address, ts=clock.now(), tx=f"0x{wl.address[-2:]}{i}", size=str(100 + i))
            for i in range(2)
        ]
    await asyncio.gather(*[rt.monitor.poll_once(await rt.registry.get(wl.id)) for wl in wallets])
    sigs = await signals(rt)
    assert len(sigs) == 8
    spent = sum((s.filled_cost for s in sigs if s.filled_cost is not None), D(0))
    acct = await rt.ledger.get_account(rt.account_id)
    assert spent <= D("25")
    assert acct.cash == D("30") - spent and acct.cash >= D("5")
    assert acct.reserved == 0
    assert all(s.status in (SignalStatus.EXECUTED, SignalStatus.REJECTED) for s in sigs)
    assert await rt.ledger.verify(rt.account_id) == []


# ------------------------------------------------------------------- equity and marks


async def test_unmarked_position_then_settlement_gives_exact_equity(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # 100 YES for $40
    world.resolution = {YES: "0", NO: "1"}
    # Resolved market: the CLOB book is gone and nothing is cached.
    rest.books.pop(YES)
    rt.market_data._books.pop(YES, None)
    before = await rt.copy.portfolio()
    # Current behaviour before settlement runs: unknown mark => carried at cost, flagged.
    assert before.unmarked_positions == 1
    assert before.positions[0].mark is None
    assert before.equity == D("1000")
    await rt.hedger.settle_resolved()
    after = await rt.copy.portfolio()
    assert after.positions == () and after.unmarked_positions == 0
    assert after.equity == after.cash == D("960")
    assert after.realized_pnl == D("-40")
    assert await rt.ledger.verify(rt.account_id) == []


async def test_vanished_book_is_unmarked_not_valued_at_hour_old_mark(env, world):
    rt, clock, rest = env
    w = await follow(rt, world)
    await trade(rt, clock, world, w)  # caches YES book (mid .395)
    rest.books.pop(YES)  # e.g. market closed: CLOB no longer serves the book
    clock.advance(3600)
    view = await rt.copy.portfolio()
    [pos] = view.positions
    assert pos.mark is None
    assert view.unmarked_positions == 1
