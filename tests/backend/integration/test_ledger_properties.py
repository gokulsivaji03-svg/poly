"""Property-based accounting invariants over random strategy event sequences (PostgreSQL)."""

import asyncio
from datetime import timedelta
from decimal import Decimal

from app.core.clock import ManualClock
from app.db.models import AccountMode, LotOrigin, OrderKind, OrderStatus
from app.domain.market import Side
from app.services.ledger import PortfolioLedger
from app.services.paper_execution import ExecutionRequest, PaperExecutionEngine
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from conftest import TEST_DB, truncate_all
from factories import COND, NO, NOW, YES, D, FakeBooks, book, fees

price = st.decimals(min_value="0.05", max_value="0.95", places=2)
op = st.one_of(
    st.tuples(
        st.just("buy"),
        st.sampled_from([YES, NO]),
        st.decimals(min_value="1", max_value="400", places=2),
        price,
        st.sampled_from(["0", "0.05"]),
    ),
    st.tuples(
        st.just("sell"),
        st.sampled_from([YES, NO]),
        st.decimals(min_value="1", max_value="500", places=2),
        price,
        st.sampled_from(["0", "0.05"]),
    ),
    st.tuples(st.just("match"), st.just(YES), st.just(D(0)), price, st.just("0")),
    st.tuples(st.just("merge"), st.just(YES), st.just(D(0)), price, st.just("0")),
)


async def run_sequence(ops) -> None:
    engine = create_async_engine(TEST_DB, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    await truncate_all(sm)
    clock = ManualClock(NOW)
    books = FakeBooks(clock)
    ledger = PortfolioLedger(sm, clock)
    px = PaperExecutionEngine(sm, ledger, books, clock)
    start = D("1000")
    acct = await ledger.ensure_account(AccountMode.PAPER, start)
    try:
        for i, (kind, token, amount, p, rate) in enumerate(ops):
            clock.advance(1)
            books.set(
                book(
                    token,
                    asks=[(str(p), "300"), (str(min(p + D("0.03"), D("0.99"))), "300")],
                    bids=[(str(p), "300")],
                )
            )
            if kind == "buy":
                res = await ledger.reserve(
                    acct, amount, min_reserve=D("0"), key=f"r{i}", purpose="prop"
                )
                if res is None:
                    a = await ledger.get_account(acct)
                    assert a.cash - a.reserved < amount  # refusal only when truly short
                    continue
                r = await px.execute(
                    ExecutionRequest(
                        account_id=acct,
                        idempotency_key=f"o{i}",
                        kind=OrderKind.COPY_BUY,
                        side=Side.BUY,
                        condition_id=COND,
                        token_id=token,
                        fee_model=fees(rate),
                        max_book_age=timedelta(seconds=15),
                        budget=amount,
                        reservation_id=res.id,
                        origin=LotOrigin.COPY if token == YES else LotOrigin.HEDGE,
                    )
                )
                if r.filled:
                    assert r.fill.cash_cost <= amount
            elif kind == "sell":
                lots = await ledger.open_lots(acct)
                free = sum(
                    (
                        lt.shares_remaining - lt.matched_shares
                        for lt in lots
                        if lt.token_id == token
                    ),
                    Decimal(0),
                )
                r = await px.execute(
                    ExecutionRequest(
                        account_id=acct,
                        idempotency_key=f"o{i}",
                        kind=OrderKind.COPY_SELL,
                        side=Side.SELL,
                        condition_id=COND,
                        token_id=token,
                        fee_model=fees(rate),
                        max_book_age=timedelta(seconds=15),
                        target_shares=amount,
                    )
                )
                if amount > free:
                    assert r.status in (OrderStatus.FAILED, OrderStatus.REJECTED)
            elif kind == "match":
                lots = await ledger.open_lots(acct)
                ys = [
                    lt
                    for lt in lots
                    if lt.token_id == YES and lt.shares_remaining > lt.matched_shares
                ]
                ns = [
                    lt
                    for lt in lots
                    if lt.token_id == NO and lt.shares_remaining > lt.matched_shares
                ]
                if ys and ns:
                    n = ns[0]
                    qty = min(
                        n.shares_remaining - n.matched_shares,
                        ys[0].shares_remaining - ys[0].matched_shares,
                    )
                    async with sm() as s, s.begin():
                        await ledger.match_pairs(
                            s, allocations=[(ys[0].id, qty)], hedge_lot_id=n.id
                        )
            else:
                await ledger.merge_matched(COND, acct)

            a = await ledger.get_account(acct)
            assert a.cash >= 0 and 0 <= a.reserved <= a.cash
            assert await ledger.verify(acct) == []
            # No value from nothing: equity at cost == start + realized - (fees already in cost)
            view = await ledger.portfolio(acct, {})
            assert view.equity == start + a.realized_pnl
    finally:
        await engine.dispose()


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(op, min_size=1, max_size=12))
def test_random_sequences_preserve_ledger_invariants(ops):
    asyncio.run(run_sequence(ops))
