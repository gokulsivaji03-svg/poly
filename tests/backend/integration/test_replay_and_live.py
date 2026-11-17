"""Historical replay (no look-ahead, isolated) and live-execution gating."""

from dataclasses import dataclass, replace
from datetime import timedelta

import pytest
import respx
from app.config import ExecutionMode
from app.core.clock import ManualClock
from app.db.models import (
    AccountMode,
    BookSnapshot,
    OrderKind,
    OrderStatus,
    ReplayRun,
    SourceEvent,
    WalletEquitySnapshot,
)
from app.domain.market import Side
from app.execution.live import LiveExecutor
from app.execution.orders import ExecutionRequest
from app.integrations.http import ResilientHttp
from app.integrations.polymarket.geoblock import GeoblockChecker
from app.integrations.polymarket.live import LiveGate, LiveTradingUnavailable
from app.runtime import Runtime
from app.services.ledger import PortfolioLedger
from app.services.replay import ReplayParams
from scenario import (
    COND,
    NO,
    WALLET_A,
    YES,
    FakeRest,
    World,
    activity,
    make_runtime,
    scenario_settings,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW, D, FakeBooks, fees, market

START = NOW


def snap(token: str, at, asks):
    return BookSnapshot(
        token_id=token,
        condition_id=COND,
        received_at=at,
        bids={"levels": []},
        asks={
            "levels": [[p, s] for p, s in asks],
            "market": market(tokens=(YES, NO))
            .model_copy(update={"condition_id": COND})
            .model_dump(mode="json"),
        },
        tick_size=D("0.01"),
        min_order_size=D("0"),
        hash=f"{token}-{at}",
    )


def trade(wallet_id: int, at, *, price="0.40", size="500", key="k") -> SourceEvent:
    return SourceEvent(
        wallet_id=wallet_id,
        event_key=key,
        occurrence=0,
        type="TRADE",
        raw_type="TRADE",
        side="BUY",
        condition_id=COND,
        token_id=YES,
        outcome="Yes",
        size=D(size),
        usdc_size=D(size) * D(price),
        price=D(price),
        tx_hash=key,
        block_ts=at,
        detected_at=at,
        title="Will it rain?",
        is_backfill=True,
    )


@pytest.fixture
def world():
    return World()


@pytest.fixture
async def rt(sm, world):
    clock = ManualClock(NOW + timedelta(days=1))  # "today" is after the replay window
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        runtime = await make_runtime(TEST_DB, clock, FakeRest(clock), create_async_engine(TEST_DB))
        yield runtime
        await runtime.stop()


async def test_replay_uses_only_past_snapshots_and_never_touches_live(rt):
    w = await rt.registry.add(WALLET_A)
    async with rt.sm() as s, s.begin():
        s.add(
            WalletEquitySnapshot(
                wallet_id=w.id,
                observed_at=START - timedelta(minutes=1),
                positions_value=D("4000"),
                cash_balance=D("6000"),
                coverage="POSITIONS_AND_CASH",
                open_positions=3,
            )
        )
        s.add_all(
            [
                snap(YES, START + timedelta(seconds=10), [("0.40", "1000")]),
                snap(YES, START + timedelta(seconds=100), [("0.90", "1000")]),  # future vs trade 1
                snap(NO, START + timedelta(seconds=10), [("0.62", "1000")]),
            ]
        )
        s.add_all(
            [
                trade(w.id, START + timedelta(seconds=5), key="early"),  # before any snapshot
                trade(w.id, START + timedelta(seconds=20), key="mid"),  # sees 0.40 only
            ]
        )
    live_before = await rt.ledger.get_account(rt.account_id)
    run_id = await rt.replay.submit(
        ReplayParams(
            start=START,
            end=START + timedelta(minutes=5),
            wallet_ids=[w.id],
            starting_bankroll=D("500"),
            detection_delay_s=2,
            prices="recorded",
        )
    )
    await rt.replay.wait()
    async with rt.sm() as s:
        run = await s.get(ReplayRun, run_id)
        schemas = (
            await s.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'replay_%'"
                )
            )
        ).all()
    assert run.status == "COMPLETED", run.error
    sigs = run.result["signals"]
    assert [sg["status"] for sg in sigs] == ["REJECTED", "EXECUTED"]
    assert sigs[0]["reasons"][-1] == "MARKET_UNAVAILABLE"
    assert D(sigs[1]["avg_fill_price"]) == D("0.4")  # never the future 0.90 book
    assert run.result["summary"]["ledger_ok"] is True
    assert run.result["summary"]["events_replayed"] == 2
    assert schemas == []  # isolated schema dropped
    live_after = await rt.ledger.get_account(rt.account_id)
    assert (live_after.cash, live_after.realized_pnl) == (
        live_before.cash,
        live_before.realized_pnl,
    )
    assert await rt.ledger.open_lots(rt.account_id) == []


async def test_history_replay_fills_at_minute_bars_and_settles_at_resolution(rt, world):
    w = await rt.registry.add(WALLET_A)
    async with rt.sm() as s, s.begin():
        s.add(
            WalletEquitySnapshot(
                wallet_id=w.id,
                observed_at=START + timedelta(hours=2),
                positions_value=D("4000"),
                cash_balance=D("6000"),
                coverage="POSITIONS_AND_CASH",
                open_positions=3,
            )
        )
    world.activity[WALLET_A] = [
        activity(WALLET_A, ts=START + timedelta(seconds=60), price="0.40", tx="0xh1"),
        activity(WALLET_A, ts=START + timedelta(seconds=300), price="0.40", tx="0xh2"),
    ]
    world.prices[YES] = [(START, "0.40"), (START + timedelta(seconds=240), "0.50")]
    world.resolution = {YES: "1", NO: "0"}
    world.closed_at = START + timedelta(minutes=30)
    run_id = await rt.replay.submit(
        ReplayParams(
            start=START,
            end=START + timedelta(hours=3),
            wallet_ids=[w.id],
            starting_bankroll=D("500"),
        )
    )
    await rt.replay.wait()
    async with rt.sm() as s:
        run = await s.get(ReplayRun, run_id)
    assert run.status == "COMPLETED", run.error
    sigs, summary = run.result["signals"], run.result["summary"]
    assert [sg["status"] for sg in sigs] == ["EXECUTED", "REJECTED"]
    assert D(sigs[0]["avg_fill_price"]) == D("0.40"), "filled at the bar before detection"
    assert sigs[1]["reasons"][-1] == "PRICE_MOVED", "the 0.50 bar is past the limit"
    assert (summary["activity_fetched"], summary["equity_backfilled"]) == (2, 1)
    assert summary["minute_bars_used"] >= 2 and summary["prices"] == "history"
    assert D(summary["realized_pnl"]) == D("15"), "25 shares bought at 0.40 paid out at 1"
    assert D(summary["final_equity"]) == D("515") and summary["ledger_ok"] is True


async def test_history_replay_sizes_from_equity_walked_back_along_pnl(rt, world):
    w = await rt.registry.add(WALLET_A)
    async with rt.sm() as s, s.begin():
        s.add(
            WalletEquitySnapshot(
                wallet_id=w.id,
                observed_at=START + timedelta(hours=2),
                positions_value=D("4000"),
                cash_balance=D("6000"),
                coverage="POSITIONS_AND_CASH",
                open_positions=3,
            )
        )
    world.pnl_points[WALLET_A] = [
        (START + timedelta(hours=h), v) for h, v in ((0, "0"), (1, "2500"), (2, "5000"))
    ]
    world.activity[WALLET_A] = [
        activity(WALLET_A, ts=START + timedelta(seconds=60), price="0.40", tx="0xp1")
    ]
    world.prices[YES] = [(START, "0.40")]
    run_id = await rt.replay.submit(
        ReplayParams(
            start=START,
            end=START + timedelta(hours=3),
            wallet_ids=[w.id],
            starting_bankroll=D("500"),
        )
    )
    await rt.replay.wait()
    async with rt.sm() as s:
        run = await s.get(ReplayRun, run_id)
    assert run.status == "COMPLETED", run.error
    [sig] = run.result["signals"]
    assert D(sig["filled_cost"]) == D("20"), "$200 of a $5,000 wallet is 4%, not 2% of $10,000"


async def test_replay_rejects_invalid_windows():
    with pytest.raises(ValueError):
        ReplayParams(start=START, end=START, wallet_ids=[1])
    with pytest.raises(ValueError):
        ReplayParams(start=START, end=START + timedelta(days=40), wallet_ids=[1])


@dataclass
class FakeAccepted:
    ok: bool = True
    order_id: str = "0xorder"
    status: str = "matched"
    making_amount: str = "20"
    taking_amount: str = "50"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def place_market_order(self, **kwargs):
        self.calls.append(kwargs)
        return FakeAccepted()


async def _live_engine(sm, clock, *, geo_blocked: bool, enabled: bool = True):
    cfg = scenario_settings(TEST_DB).model_copy(
        update={
            "execution_mode": ExecutionMode.LIVE,
            "live_trading_enabled": enabled,
            "live_private_key": "0x" + "1" * 64,
            "live_funder_address": "0x" + "2" * 40,
        }
    )
    client = FakeClient()
    http = ResilientHttp(max_retries=0)
    geo = GeoblockChecker(http, "https://geo.test/api/geoblock", clock)
    ledger = PortfolioLedger(sm, clock)
    acct = await ledger.ensure_account(AccountMode.LIVE, D("0"))
    await ledger.sync_cash(acct, D("100"), "test balance")

    async def factory():
        return client

    engine = LiveExecutor(
        sm, ledger, FakeBooks(clock), clock, gate=LiveGate(cfg, geo), client_factory=factory
    )
    return engine, client, ledger, acct, http


def live_req(acct: int, key: str) -> ExecutionRequest:
    return ExecutionRequest(
        account_id=acct,
        idempotency_key=key,
        kind=OrderKind.COPY_BUY,
        side=Side.BUY,
        condition_id=COND,
        token_id=YES,
        fee_model=fees(),
        max_book_age=timedelta(seconds=15),
        budget=D("20"),
        limit_price=D("0.45"),
    )


@pytest.mark.parametrize("blocked", [True, None])
async def test_live_orders_refused_in_restricted_or_unknown_jurisdiction(sm, clock, blocked):
    with respx.mock() as router:
        if blocked is None:
            router.get("https://geo.test/api/geoblock").respond(500)
        else:
            router.get("https://geo.test/api/geoblock").respond(
                200, json={"blocked": True, "country": "US", "region": "CA"}
            )
        engine, client, ledger, acct, http = await _live_engine(sm, clock, geo_blocked=True)
        res = await engine.execute(live_req(acct, "live-1"))
        await http.aclose()
    assert res.status is OrderStatus.REJECTED
    assert res.reason.startswith("LIVE_DISABLED")
    assert client.calls == []  # the exchange was never contacted
    assert (await ledger.get_account(acct)).cash == D("100")


async def test_live_order_placed_and_booked_when_every_gate_passes(sm, clock):
    with respx.mock() as router:
        router.get("https://geo.test/api/geoblock").respond(
            200, json={"blocked": False, "country": "XX", "region": ""}
        )
        engine, client, ledger, acct, http = await _live_engine(sm, clock, geo_blocked=False)
        res = await engine.execute(live_req(acct, "live-2"))
        await http.aclose()
    assert res.status is OrderStatus.FILLED, res.reason
    assert client.calls == [
        {
            "token_id": YES,
            "side": "BUY",
            "order_type": "FAK",
            "amount": D("20"),
            "max_price": D("0.45"),
        }
    ]
    a = await ledger.get_account(acct)
    assert a.cash == D("80")
    [lot] = await ledger.open_lots(acct)
    assert lot.shares_acquired == D("50") and lot.entry_price == D("0.4")
    assert await ledger.verify(acct) == []


def live_buy(acct: int, key: str) -> ExecutionRequest:
    return replace(live_req(acct, key), budget=D("5"))


async def test_live_orders_reuse_a_recent_geoblock_check(sm, clock):
    with respx.mock() as router:
        geo = router.get("https://geo.test/api/geoblock").respond(
            200, json={"blocked": False, "country": "XX", "region": ""}
        )
        engine, client, _ledger, acct, http = await _live_engine(sm, clock, geo_blocked=False)
        first = await engine.execute(live_buy(acct, "geo-1"))
        clock.advance(60)
        second = await engine.execute(live_buy(acct, "geo-2"))
        assert geo.call_count == 1, "second order reused the 60 s old result"
        clock.advance(31)
        third = await engine.execute(live_buy(acct, "geo-3"))
        assert geo.call_count == 2, "a result older than 90 s is refreshed"
        await http.aclose()
    assert [r.status for r in (first, second, third)] == [OrderStatus.FILLED] * 3
    assert len(client.calls) == 3


async def test_live_order_refused_on_a_recent_blocked_result_without_refetching(sm, clock):
    with respx.mock() as router:
        geo = router.get("https://geo.test/api/geoblock").respond(
            200, json={"blocked": True, "country": "US", "region": "CA"}
        )
        engine, client, _ledger, acct, http = await _live_engine(sm, clock, geo_blocked=True)
        await engine.execute(live_buy(acct, "blk-1"))
        res = await engine.execute(live_buy(acct, "blk-2"))
        await http.aclose()
    assert geo.call_count == 1
    assert res.status is OrderStatus.REJECTED and res.reason.startswith("LIVE_DISABLED")
    assert client.calls == []


async def test_live_runtime_refuses_to_start_workers_when_geoblocked(sm):
    clock = ManualClock(NOW)
    world = World()
    cfg = scenario_settings(TEST_DB).model_copy(
        update={
            "execution_mode": ExecutionMode.LIVE,
            "live_trading_enabled": True,
            "run_workers": True,
        }
    )
    with respx.mock(assert_all_called=False) as router:
        world.install(router)  # geoblock route reports US/CA
        rt = await Runtime.create(
            cfg,
            clock=clock,
            engine=create_async_engine(TEST_DB),
            http=ResilientHttp(max_retries=0),
            rest_books=FakeRest(clock),
            enable_stream=False,
        )
        await rt.start()
        venues = await rt.venue_status()
        await rt.stop()
    assert rt.supervisor.is_leader is False
    assert all(st.state == "standby" for st in rt.supervisor.status.values())
    assert "Geoblocked jurisdiction: US/CA" in (rt.live_block_reason or "")
    assert "Live credentials not configured" in (rt.live_block_reason or "")
    intl, us = venues
    assert intl["live_enabled"] is False and us["live_supported"] is False


async def test_paper_engine_cannot_be_constructed_live(sm, clock):
    from app.execution.paper import PaperExecutor

    with pytest.raises(ValueError):
        PaperExecutor(
            sm, PortfolioLedger(sm, clock), FakeBooks(clock), clock, mode=ExecutionMode.LIVE
        )


async def test_uncertain_live_submission_retains_reservation_and_never_resubmits(sm, clock):
    with respx.mock() as router:
        router.get("https://geo.test/api/geoblock").respond(
            200, json={"blocked": False, "country": "XX", "region": ""}
        )
        engine, client, ledger, acct, http = await _live_engine(sm, clock, geo_blocked=False)
        reservation = await ledger.reserve(
            acct, D("20"), min_reserve=D("0"), key="uncertain", purpose="test"
        )
        assert reservation is not None
        req = replace(live_req(acct, "uncertain"), reservation_id=reservation.id)

        async def timeout(**kwargs):
            client.calls.append(kwargs)
            raise TimeoutError("response lost after submission")

        client.place_market_order = timeout
        for _ in range(2):
            with pytest.raises(LiveTradingUnavailable):
                await engine.execute(req)
        with pytest.raises(LiveTradingUnavailable):
            await engine.recover_pending()
        await http.aclose()
    assert len(client.calls) == 1
    account = await ledger.get_account(acct)
    assert account.cash == D("100") and account.reserved == D("20")
