"""PaperExecutor: simulated immediate-or-cancel execution against live order books.

Lifecycle per order (idempotent on ``idempotency_key``):

1. Insert ``PENDING`` order (committed) — survives a crash for recovery.
2. Wait the configured execution delay, then fetch a *fresh* book (reject if stale).
3. Walk the book with fees, slippage, limit price, share step and minimum order size.
4. Book the fill, lot/closures, cash entry, reservation consumption and the order's final
   status in **one** transaction. The unfilled remainder is cancelled (FAK semantics).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ExecutionMode
from app.core.clock import Clock
from app.core.money import ONE, ZERO
from app.db.models import Lot, LotClosure, Order, OrderStatus
from app.domain.fills import FillSimulation, simulate_buy, simulate_sell
from app.domain.market import BookLevel, OrderBook, Side
from app.execution.orders import (
    BookSource,
    DuplicateInFlight,
    ExecutionRequest,
    ExecutionResult,
    ObtainedFill,
)
from app.services.ledger import LedgerError, PortfolioLedger

log = logging.getLogger(__name__)

MAX_PRICE = Decimal("0.999")


def _cap_allocations(
    allocations: tuple[tuple[int, Decimal], ...], delivered: Decimal
) -> list[tuple[int, Decimal]]:
    """Trim the planned allocations to the shares actually delivered (partial fills)."""
    out: list[tuple[int, Decimal]] = []
    left = delivered
    for lot_id, shares in allocations:
        if left <= ZERO:
            break
        take = min(shares, left)
        out.append((lot_id, take))
        left -= take
    return out


def apply_slippage(book: OrderBook, bps: int) -> OrderBook:
    """Adverse-shift every level by ``bps`` basis points (asks up, bids down)."""
    if bps == 0:
        return book
    k = Decimal(bps) / Decimal(10_000)
    asks = tuple(
        BookLevel(price=min(MAX_PRICE, lv.price * (ONE + k)), size=lv.size) for lv in book.asks
    )
    bids = tuple(
        BookLevel(price=max(Decimal("0.001"), lv.price * (ONE - k)), size=lv.size)
        for lv in book.bids
    )
    return book.model_copy(update={"asks": asks, "bids": bids})


class PaperExecutor:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        ledger: PortfolioLedger,
        books: BookSource,
        clock: Clock,
        *,
        mode: ExecutionMode = ExecutionMode.PAPER,
    ) -> None:
        if mode is ExecutionMode.LIVE and type(self) is PaperExecutor:
            raise ValueError("PaperExecutor never runs in LIVE mode")
        self._sm = sm
        self._ledger = ledger
        self._books = books
        self._clock = clock
        self._mode = mode
        self._inflight: set[str] = set()

    async def execute(
        self,
        req: ExecutionRequest,
        *,
        delay_ms: int = 0,
        slippage_bps: int = 0,
        share_step: Decimal = Decimal("0.01"),
    ) -> ExecutionResult:
        key = req.idempotency_key
        if key in self._inflight:
            raise DuplicateInFlight(key)
        self._inflight.add(key)
        try:
            return await self._execute_new(req, delay_ms, slippage_bps, share_step)
        finally:
            self._inflight.discard(key)

    async def _execute_new(
        self, req: ExecutionRequest, delay_ms: int, slippage_bps: int, share_step: Decimal
    ) -> ExecutionResult:

        started = self._clock.now()
        async with self._sm() as s, s.begin():
            order_id = await s.scalar(
                pg_insert(Order)
                .values(
                    account_id=req.account_id,
                    idempotency_key=req.idempotency_key,
                    mode=self._mode.value,
                    kind=req.kind,
                    side=req.side.value,
                    condition_id=req.condition_id,
                    token_id=req.token_id,
                    requested_budget=req.budget,
                    requested_shares=req.target_shares,
                    limit_price=req.limit_price,
                    status=OrderStatus.PENDING,
                    reservation_id=req.reservation_id,
                    signal_id=req.signal_id,
                    hedge_id=req.hedge_id,
                    created_at=started,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(Order.id)
            )
        if order_id is None:  # this idempotency key was executed before
            prior = await self._existing(req.idempotency_key)
            assert prior is not None
            return prior

        obtained = await self._obtain_fill(req, delay_ms, slippage_bps, share_step)
        if isinstance(obtained, str):
            return await self._reject(order_id, req, obtained)
        fill = obtained.fill
        status = self._fill_status(req, fill, share_step)
        latency_ms = int((self._clock.now() - started).total_seconds() * 1000)
        try:
            async with self._sm() as s, s.begin():
                order = await s.get(Order, order_id, with_for_update=True)
                assert order is not None
                lot_id: int | None = None
                closures: list[LotClosure] = []
                if req.side is Side.BUY:
                    lot: Lot = await self._ledger.book_buy(
                        s,
                        order=order,
                        fill=fill,
                        origin=req.origin,
                        outcome=req.outcome,
                        question=req.question,
                        source_wallet=req.source_wallet,
                        book_hash=obtained.book_hash,
                        book_received_at=obtained.book_received_at,
                    )
                    lot_id = lot.id
                    if req.match_allocations:
                        # Hedge leg: pair the delivered shares with primary lots in the same
                        # transaction, so a hedge is never booked without its matching.
                        await self._ledger.match_pairs(
                            s,
                            allocations=_cap_allocations(req.match_allocations, fill.net_shares),
                            hedge_lot_id=lot.id,
                        )
                else:
                    closures = await self._ledger.book_sell(
                        s,
                        order=order,
                        fill=fill,
                        allow_matched=req.allow_matched,
                        lot_ids=req.lot_ids,
                        book_hash=obtained.book_hash,
                        book_received_at=obtained.book_received_at,
                    )
                order.status = status
                order.completed_at = self._clock.now()
                order.exec_latency_ms = latency_ms
                order.book_age_ms = obtained.book_age_ms
                if status is OrderStatus.PARTIALLY_FILLED:
                    order.reason = "Remainder cancelled (insufficient depth within limit)"
        except LedgerError as exc:
            log.warning("ledger rejected fill", extra={"order_id": order_id, "error": str(exc)})
            return await self._reject(order_id, req, f"LEDGER: {exc}", failed=True)
        return ExecutionResult(
            order_id=order_id,
            status=status,
            fill=fill,
            lot_id=lot_id,
            closure_ids=tuple(c.id for c in closures),
            reason=None if status is OrderStatus.FILLED else "PARTIAL_FILL",
        )

    async def _obtain_fill(
        self, req: ExecutionRequest, delay_ms: int, slippage_bps: int, share_step: Decimal
    ) -> ObtainedFill | str:
        """Paper: walk a fresh order book. Returns a rejection reason string on failure."""
        if delay_ms:
            await self._clock.sleep(delay_ms / 1000)
        book = await self._books.get_book(req.token_id, fresh=True)
        now = self._clock.now()
        if book is None:
            return "NO_BOOK"
        if book.is_stale(now, req.max_book_age):
            return "STALE_BOOK"
        if book.token_id != req.token_id:
            return "BOOK_TOKEN_MISMATCH"
        walked = apply_slippage(book, slippage_bps)
        if req.side is Side.BUY:
            budget = req.budget
            if req.reservation_id is not None:
                res_amount = await self._reservation_amount(req.reservation_id)
                budget = res_amount if budget is None else min(budget, res_amount)
            fill = simulate_buy(
                walked,
                req.fee_model,
                budget=budget,
                target_net_shares=req.target_shares,
                limit_price=req.limit_price,
                share_step=share_step,
            )
        else:
            if req.target_shares is None:
                raise ValueError("SELL requires target_shares")
            fill = simulate_sell(
                walked,
                req.fee_model,
                shares=req.target_shares,
                limit_price=req.limit_price,
                share_step=share_step,
            )
        if not fill.filled:
            return fill.reasons[0] if fill.reasons else "NO_FILL"
        if fill.gross_shares < book.min_order_size:
            return f"BELOW_MIN_ORDER_SIZE {fill.gross_shares} < {book.min_order_size}"
        if fill.notional < req.min_order_usd:
            return f"BELOW_MIN_ORDER_USD {fill.notional:.2f}"
        return ObtainedFill(
            fill, book.hash, book.received_at, int(book.age(now).total_seconds() * 1000)
        )

    @staticmethod
    def _fill_status(req: ExecutionRequest, fill: FillSimulation, step: Decimal) -> OrderStatus:
        if req.side is Side.SELL or req.target_shares is not None:
            assert req.target_shares is not None
            got = fill.net_shares if req.side is Side.BUY else fill.gross_shares
            return (
                OrderStatus.FILLED
                if req.target_shares - got < step
                else (OrderStatus.PARTIALLY_FILLED)
            )
        assert req.budget is not None
        # Filled if what is left of the budget could not buy one more increment at the
        # worst executed price.
        leftover = req.budget - fill.cash_cost
        worst = fill.worst_price or ZERO
        return (
            OrderStatus.FILLED
            if leftover < worst * step * 2 + Decimal("0.01")
            else (OrderStatus.PARTIALLY_FILLED)
        )

    async def _reservation_amount(self, reservation_id: int) -> Decimal:
        from app.db.models import CashReservation

        async with self._sm() as s:
            amount = await s.scalar(
                select(CashReservation.amount).where(CashReservation.id == reservation_id)
            )
            if amount is None:
                raise LedgerError("reservation not found")
            return amount

    async def _reject(
        self, order_id: int, req: ExecutionRequest, reason: str, *, failed: bool = False
    ) -> ExecutionResult:
        status = OrderStatus.FAILED if failed else OrderStatus.REJECTED
        async with self._sm() as s, s.begin():
            await s.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(status=status, reason=reason, completed_at=self._clock.now())
            )
            if req.reservation_id is not None:
                await self._ledger.release_in(s, req.reservation_id)
        return ExecutionResult(order_id=order_id, status=status, reason=reason)

    async def _existing(self, key: str) -> ExecutionResult | None:
        async with self._sm() as s:
            order = await s.scalar(select(Order).where(Order.idempotency_key == key))
            if order is None:
                return None
            lot_id = await s.scalar(select(Lot.id).where(Lot.order_id == order.id))
        if order.status != OrderStatus.PENDING:
            return ExecutionResult(
                order.id,
                OrderStatus(order.status),
                lot_id=lot_id,
                reason=order.reason,
                replayed=True,
            )
        # PENDING and not in flight in this (leader) process: a crash happened between insert
        # and booking. Nothing was booked (booking is atomic with the status change), so fail
        # the order and free its reservation.
        async with self._sm() as s, s.begin():
            await s.execute(
                update(Order)
                .where(Order.id == order.id, Order.status == OrderStatus.PENDING)
                .values(
                    status=OrderStatus.FAILED,
                    reason="RECOVERED_INCOMPLETE",
                    completed_at=self._clock.now(),
                )
            )
            if order.reservation_id is not None:
                await self._ledger.release_in(s, order.reservation_id)
        return ExecutionResult(
            order.id, OrderStatus.FAILED, reason="RECOVERED_INCOMPLETE", replayed=True
        )

    async def recover_pending(self) -> int:
        """Startup reconciliation: fail and unreserve orders left PENDING by a crash."""
        async with self._sm() as s:
            keys = (
                await s.scalars(
                    select(Order.idempotency_key).where(Order.status == OrderStatus.PENDING)
                )
            ).all()
        for key in keys:
            await self._existing(key)
        return len(keys)
