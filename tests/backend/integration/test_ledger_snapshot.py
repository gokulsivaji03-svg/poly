"""The ledger version: every committed write to an account, its lots or its orders bumps it,
so the copy path can reuse its snapshot of them until one lands, from any process."""

from datetime import timedelta

import pytest
from app.db.base import Base
from app.db.models import AccountMode, Lot, LotOrigin, OrderKind
from app.domain.market import Side
from app.engine.portfolio import PortfolioReader
from app.execution.orders import ExecutionRequest
from app.execution.paper import PaperExecutor
from app.services.ledger import PortfolioLedger
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import TEST_DB
from factories import COND, YES, D, FakeBooks, book, fees


@pytest.fixture
def ledger(sm, clock):
    return PortfolioLedger(sm, clock)


def executor(sm, ledger, clock):
    books = FakeBooks(clock)
    books.set(book(YES, asks=[("0.40", "1000")], bids=[("0.38", "1000")]))
    return PaperExecutor(sm, ledger, books, clock)


async def buy(sm, ledger, clock, acct, key):
    res = await ledger.reserve(acct, D("40"), min_reserve=D("0"), key=f"r-{key}", purpose="t")
    assert res is not None
    return await executor(sm, ledger, clock).execute(
        ExecutionRequest(
            account_id=acct,
            idempotency_key=key,
            kind=OrderKind.COPY_BUY,
            side=Side.BUY,
            condition_id=COND,
            token_id=YES,
            fee_model=fees(),
            max_book_age=timedelta(seconds=15),
            budget=D("40"),
            reservation_id=res.id,
            origin=LotOrigin.COPY,
            limit_price=D("0.99"),
        )
    )


async def test_every_committed_ledger_write_bumps_the_version(sm, ledger, clock):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    v0 = await ledger.ledger_version(acct)
    await ledger.reserve(acct, D("5"), min_reserve=D("0"), key="r", purpose="t")
    v1 = await ledger.ledger_version(acct)
    assert v1 > v0, "an account update"
    await buy(sm, ledger, clock, acct, "o1")
    v2 = await ledger.ledger_version(acct)
    assert v2 > v1, "an order, a lot and the cash they moved"
    async with sm() as s, s.begin():
        await s.execute(update(Lot).values(matched_shares=Lot.matched_shares))
    v3 = await ledger.ledger_version(acct)
    assert v3 > v2, "a lot alone"
    async with sm() as s:
        await s.execute(update(Lot).values(matched_shares=Lot.matched_shares))
        await s.rollback()
    assert await ledger.ledger_version(acct) == v3, "a rolled-back write"


async def test_the_reader_reuses_its_snapshot_until_another_process_writes(sm, ledger, clock):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    await buy(sm, ledger, clock, acct, "o1")
    loads = 0
    load = ledger.snapshot

    async def counted(account_id):
        nonlocal loads
        loads += 1
        return await load(account_id)

    ledger.snapshot = counted
    reader = PortfolioReader(ledger, None, clock, acct)  # type: ignore[arg-type]
    first = await reader.snapshot()
    assert (len(first.lots), first.copy_buys) == (1, {COND: 1})
    assert await reader.snapshot() is first and loads == 1

    other = create_async_engine(TEST_DB)
    try:
        other_ledger = PortfolioLedger(async_sessionmaker(other, expire_on_commit=False), clock)
        await other_ledger.reserve(acct, D("7"), min_reserve=D("0"), key="api", purpose="t")
    finally:
        await other.dispose()
    fresh = await reader.snapshot()
    assert loads == 2 and fresh.account.reserved == D("7")


async def test_refresh_soon_leaves_the_next_read_a_current_snapshot(sm, ledger, clock):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    reader = PortfolioReader(ledger, None, clock, acct)  # type: ignore[arg-type]
    await reader.snapshot()
    await buy(sm, ledger, clock, acct, "o1")
    reader.refresh_soon()
    assert reader._reload is not None
    await reader._reload
    assert reader._snap is not None and len(reader._snap.lots) == 1
    assert (await reader.snapshot()) is reader._snap


async def test_a_replay_schema_bumps_its_own_accounts_not_the_public_ones(sm, ledger, clock):
    public_acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    public_version = await ledger.ledger_version(public_acct)
    base = create_async_engine(TEST_DB)
    async with base.begin() as conn:
        await conn.execute(text('CREATE SCHEMA "replay_t"'))
    replay = base.execution_options(schema_translate_map={None: "replay_t"})
    try:
        async with replay.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        rsm = async_sessionmaker(replay, expire_on_commit=False)
        rledger = PortfolioLedger(rsm, clock)
        acct = await rledger.ensure_account(AccountMode.REPLAY, D("1000"))
        await buy(rsm, rledger, clock, acct, "o1")
        before = await rledger.ledger_version(acct)
        async with rsm() as s, s.begin():
            await s.execute(update(Lot).values(matched_shares=Lot.matched_shares))
        assert await rledger.ledger_version(acct) == before + 1
        async with sm() as s:
            assert (
                await s.scalar(
                    text("SELECT ledger_version FROM public.accounts WHERE id = :id"),
                    {"id": public_acct},
                )
                == public_version
            )
    finally:
        async with base.begin() as conn:
            await conn.execute(text('DROP SCHEMA "replay_t" CASCADE'))
        await base.dispose()
