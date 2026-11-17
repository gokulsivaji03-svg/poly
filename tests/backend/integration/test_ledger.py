import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from app.db.models import AccountMode, LotOrigin, OrderKind, OrderStatus
from app.domain.market import Side
from app.execution.orders import ExecutionRequest
from app.execution.paper import PaperExecutor
from app.services.ledger import PortfolioLedger

from factories import COND, NO, NOW, YES, D, FakeBooks, book, fees

AGE = timedelta(seconds=15)


@pytest.fixture
def ledger(sm, clock):
    return PortfolioLedger(sm, clock)


@pytest.fixture
def books(clock):
    fb = FakeBooks(clock)
    fb.set(book(YES, asks=[("0.40", "1000")], bids=[("0.38", "1000")]))
    fb.set(book(NO, asks=[("0.55", "1000")], bids=[("0.53", "1000")]))
    return fb


@pytest.fixture
def engine(sm, ledger, books, clock):
    return PaperExecutor(sm, ledger, books, clock)


async def buy(engine, ledger, acct, token, budget, key, *, origin=LotOrigin.COPY, rate="0"):
    res = await ledger.reserve(acct, D(budget), min_reserve=D("0"), key=f"r-{key}", purpose="t")
    assert res is not None
    return await engine.execute(
        ExecutionRequest(
            account_id=acct,
            idempotency_key=key,
            kind=OrderKind.COPY_BUY,
            side=Side.BUY,
            condition_id=COND,
            token_id=token,
            fee_model=fees(rate),
            max_book_age=AGE,
            budget=D(budget),
            reservation_id=res.id,
            origin=origin,
            limit_price=D("0.99"),
        )
    )


async def sell(engine, acct, token, shares, key, *, allow_matched=False):
    return await engine.execute(
        ExecutionRequest(
            account_id=acct,
            idempotency_key=key,
            kind=OrderKind.COPY_SELL,
            side=Side.SELL,
            condition_id=COND,
            token_id=token,
            fee_model=fees(),
            max_book_age=AGE,
            target_shares=D(shares),
            allow_matched=allow_matched,
        )
    )


async def test_buy_books_lot_cash_and_consumes_reservation(ledger, engine):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    r = await buy(engine, ledger, acct, YES, "40", "o1")
    assert r.status is OrderStatus.FILLED
    a = await ledger.get_account(acct)
    assert a.cash == D("960") and a.reserved == 0
    lots = await ledger.open_lots(acct)
    assert len(lots) == 1 and lots[0].shares_remaining == D("100")
    assert await ledger.verify(acct) == []


async def test_concurrent_reservations_never_overspend(ledger):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("100"))
    results = await asyncio.gather(
        *[
            ledger.reserve(acct, D("10"), min_reserve=D("0"), key=f"k{i}", purpose="t")
            for i in range(25)
        ]
    )
    granted = [r for r in results if r is not None]
    assert len(granted) == 10
    a = await ledger.get_account(acct)
    assert a.reserved == D("100") and a.cash - a.reserved == 0
    assert await ledger.verify(acct) == []


async def test_reservation_is_idempotent_and_respects_cash_floor(ledger):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("100"))
    a = await ledger.reserve(acct, D("30"), min_reserve=D("25"), key="same", purpose="t")
    b = await ledger.reserve(acct, D("30"), min_reserve=D("25"), key="same", purpose="t")
    assert a == b
    assert await ledger.reserve(acct, D("50"), min_reserve=D("25"), key="x", purpose="t") is None
    assert (await ledger.get_account(acct)).reserved == D("30")


async def test_partial_fill_cancels_remainder_and_frees_cash(ledger, engine, books):
    books.set(book(YES, asks=[("0.40", "10")]))
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    r = await buy(engine, ledger, acct, YES, "40", "p1")
    assert r.status is OrderStatus.PARTIALLY_FILLED
    assert r.fill.gross_shares == D("10")
    a = await ledger.get_account(acct)
    assert a.reserved == 0 and a.cash == D("996")
    assert await ledger.verify(acct) == []


async def test_stale_book_rejects_and_releases(ledger, engine, books):
    books.set(book(YES, asks=[("0.40", "100")], received_at=NOW - timedelta(minutes=5)), pin=True)
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    r = await buy(engine, ledger, acct, YES, "40", "s1")
    assert r.status is OrderStatus.REJECTED and r.reason == "STALE_BOOK"
    assert (await ledger.get_account(acct)).reserved == 0


async def test_idempotent_execution_never_double_books(ledger, engine):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    first = await buy(engine, ledger, acct, YES, "40", "dup")
    again = await engine.execute(
        ExecutionRequest(
            account_id=acct,
            idempotency_key="dup",
            kind=OrderKind.COPY_BUY,
            side=Side.BUY,
            condition_id=COND,
            token_id=YES,
            fee_model=fees(),
            max_book_age=AGE,
            budget=D("40"),
        )
    )
    assert again.replayed and again.order_id == first.order_id
    assert len(await ledger.open_lots(acct)) == 1
    assert (await ledger.get_account(acct)).cash == D("960")


async def test_sell_fifo_realizes_pnl_and_never_oversells(ledger, engine, books):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    await buy(engine, ledger, acct, YES, "40", "b1")  # 100 @ 0.40
    books.set(book(YES, asks=[("0.50", "1000")], bids=[("0.48", "1000")]))
    await buy(engine, ledger, acct, YES, "50", "b2")  # 100 @ 0.50
    books.set(book(YES, bids=[("0.60", "1000")]))
    r = await sell(engine, acct, YES, "150", "s1")
    assert r.status is OrderStatus.FILLED
    a = await ledger.get_account(acct)
    # 100 @ cost 0.40 + 50 @ cost 0.50 sold at 0.60 => 90 - 65 = 25
    assert a.realized_pnl == D("25")
    over = await sell(engine, acct, YES, "51", "s2")
    assert over.status is OrderStatus.FAILED and "available" in over.reason
    assert await ledger.verify(acct) == []


async def test_matched_shares_are_protected_from_sells(ledger, engine, sm):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    y = await buy(engine, ledger, acct, YES, "40", "y")
    n = await buy(engine, ledger, acct, NO, "55", "n", origin=LotOrigin.HEDGE)
    async with sm() as s, s.begin():
        await ledger.match_pairs(s, allocations=[(y.lot_id, D("100"))], hedge_lot_id=n.lot_id)
    blocked = await sell(engine, acct, YES, "1", "s")
    assert blocked.status is OrderStatus.FAILED
    view = await ledger.portfolio(acct, {YES: D("0.39"), NO: D("0.54")})
    assert view.locked_profit == D("5")
    assert await ledger.verify(acct) == []


async def test_merge_and_settlement_conserve_cash(ledger, engine, sm):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    y = await buy(engine, ledger, acct, YES, "40", "y")
    n = await buy(engine, ledger, acct, NO, "27.5", "n", origin=LotOrigin.HEDGE)  # 50 NO
    async with sm() as s, s.begin():
        await ledger.match_pairs(s, allocations=[(y.lot_id, D("50"))], hedge_lot_id=n.lot_id)
    merged = await ledger.merge_matched(COND, acct)
    assert merged == D("50")
    a = await ledger.get_account(acct)
    # cost of merged: 50 YES @0.40 = 20 + 50 NO @0.55 = 27.5 => realized 2.5
    assert a.realized_pnl == D("2.5")
    paid = await ledger.settle_condition(COND, {YES: D("1"), NO: D("0")}, acct, "resolved YES")
    assert paid == D("50")
    a = await ledger.get_account(acct)
    assert a.cash == D("1000") - D("40") - D("27.5") + D("50") + D("50")
    assert a.realized_pnl == D("2.5") + D("30")
    assert await ledger.open_lots(acct) == []
    assert await ledger.verify(acct) == []


async def test_recover_pending_releases_reservation(ledger, engine, sm):
    from app.db.models import Order

    acct = await ledger.ensure_account(AccountMode.PAPER, D("100"))
    res = await ledger.reserve(acct, D("20"), min_reserve=D("0"), key="r", purpose="t")
    async with sm() as s, s.begin():
        s.add(
            Order(
                account_id=acct,
                idempotency_key="crashed",
                mode="PAPER",
                kind=OrderKind.COPY_BUY,
                side="BUY",
                condition_id=COND,
                token_id=YES,
                status=OrderStatus.PENDING,
                reservation_id=res.id,
            )
        )
    assert await engine.recover_pending() == 1
    assert (await ledger.get_account(acct)).reserved == 0
    assert await ledger.verify(acct) == []


async def test_reset_account(ledger, engine):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    await buy(engine, ledger, acct, YES, "40", "x")
    await ledger.reset_account(acct, D("500"))
    a = await ledger.get_account(acct)
    assert a.cash == D("500") and await ledger.open_lots(acct) == []
    assert await ledger.verify(acct) == []


async def test_equity_view_uses_marks_and_flags_unmarked(ledger, engine):
    acct = await ledger.ensure_account(AccountMode.PAPER, D("1000"))
    await buy(engine, ledger, acct, YES, "40", "x")
    view = await ledger.portfolio(acct, {YES: D("0.50")})
    assert view.positions_value == D("50") and view.equity == D("1010")
    assert view.unrealized_pnl == D("10")
    unmarked = await ledger.portfolio(acct, {})
    assert unmarked.unmarked_positions == 1 and unmarked.equity == D("1000")
    assert isinstance(view.risk_snapshot(Decimal(0)).exposure_by_token[YES], Decimal)
