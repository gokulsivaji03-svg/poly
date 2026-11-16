"""PortfolioLedger: the single accounting engine shared by copy trading and hedging.

All writes happen inside one DB transaction per business event so that cash, lots, fills and
ledger entries can never disagree. Reservations are taken with a conditional UPDATE, which
is atomic under concurrent callers without any application-level lock.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.money import ONE, ZERO, usdc_down
from app.db.models import (
    Account,
    AccountMode,
    CashReservation,
    CopySignal,
    EntryKind,
    EquitySnapshot,
    Fill,
    HedgeEvent,
    HedgePosition,
    LedgerEntry,
    Lot,
    LotClosure,
    LotOrigin,
    Order,
    ReservationStatus,
)
from app.domain.fills import FillSimulation
from app.domain.market import Side
from app.domain.risk import PortfolioSnapshot


class LedgerError(Exception):
    pass


class InsufficientInventory(LedgerError):
    pass


class ReservationError(LedgerError):
    pass


class _InsufficientCash(Exception):
    """Internal: aborts a reservation transaction so the inserted row is rolled back."""


@dataclass(frozen=True, slots=True)
class Reservation:
    id: int
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PositionView:
    condition_id: str
    token_id: str
    outcome: str
    question: str
    shares: Decimal
    matched_shares: Decimal
    cost_basis: Decimal
    avg_entry: Decimal
    mark: Decimal | None
    market_value: Decimal
    unrealized_pnl: Decimal
    lot_ids: tuple[int, ...]
    origins: tuple[str, ...]

    @property
    def unmatched_shares(self) -> Decimal:
        return self.shares - self.matched_shares

    @property
    def hedged_pct(self) -> Decimal:
        return self.matched_shares / self.shares if self.shares > ZERO else ZERO


@dataclass(frozen=True, slots=True)
class PortfolioView:
    account_id: int
    as_of: datetime
    starting_bankroll: Decimal
    cash: Decimal
    reserved: Decimal
    positions_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    locked_profit: Decimal
    fees_paid: Decimal
    positions: tuple[PositionView, ...]
    locked_by_condition: dict[str, Decimal] = field(default_factory=dict)
    unmarked_positions: int = 0

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved

    @property
    def equity(self) -> Decimal:
        return self.cash + self.positions_value

    @property
    def invested(self) -> Decimal:
        return sum((p.cost_basis for p in self.positions), ZERO)

    def risk_snapshot(self, day_pnl: Decimal) -> PortfolioSnapshot:
        by_token: dict[str, Decimal] = defaultdict(lambda: ZERO)
        by_cond: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for p in self.positions:
            by_token[p.token_id] += p.market_value
            by_cond[p.condition_id] += p.market_value
        return PortfolioSnapshot(
            equity=self.equity,
            cash=self.cash,
            reserved=self.reserved,
            exposure_total=self.positions_value + self.reserved,
            exposure_by_condition=dict(by_cond),
            exposure_by_token=dict(by_token),
            day_pnl=day_pnl,
        )


def _lot_cost_for(lot: Lot, shares: Decimal) -> Decimal:
    """Pro-rata cost of ``shares`` out of the lot's remaining shares and cost."""
    if shares >= lot.shares_remaining:
        return lot.cost_remaining
    return lot.cost_remaining * shares / lot.shares_remaining


class PortfolioLedger:
    def __init__(self, sm: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sm = sm
        self._clock = clock

    # ------------------------------------------------------------------ accounts
    async def ensure_account(
        self, mode: AccountMode, starting_bankroll: Decimal, name: str = "default"
    ) -> int:
        async with self._sm() as s, s.begin():
            existing = await s.scalar(
                select(Account.id).where(Account.mode == mode, Account.name == name)
            )
            if existing is not None:
                return existing
            stmt = (
                pg_insert(Account)
                .values(
                    mode=mode,
                    name=name,
                    starting_bankroll=starting_bankroll,
                    cash=starting_bankroll,
                    reserved=ZERO,
                    realized_pnl=ZERO,
                    fees_paid=ZERO,
                )
                .on_conflict_do_nothing(index_elements=["mode", "name"])
                .returning(Account.id)
            )
            account_id = await s.scalar(stmt)
            if account_id is None:  # lost a race with another creator
                found = await s.scalar(
                    select(Account.id).where(Account.mode == mode, Account.name == name)
                )
                assert found is not None
                return found
            s.add(
                LedgerEntry(
                    account_id=account_id,
                    kind=EntryKind.DEPOSIT,
                    amount=starting_bankroll,
                    cash_after=starting_bankroll,
                    memo="Starting bankroll",
                    created_at=self._clock.now(),
                )
            )
            return account_id

    async def reset_account(self, account_id: int, starting_bankroll: Decimal) -> None:
        """Wipe a PAPER/REPLAY account's trading history and restart from a new bankroll."""
        async with self._sm() as s, s.begin():
            acct = await s.get(Account, account_id, with_for_update=True)
            if acct is None or acct.mode == AccountMode.LIVE:
                raise LedgerError("only simulated accounts can be reset")
            lot_ids = select(Lot.id).where(Lot.account_id == account_id)
            await s.execute(delete(LotClosure).where(LotClosure.lot_id.in_(lot_ids)))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.account_id == account_id))
            hedge_ids = select(HedgePosition.id).where(HedgePosition.account_id == account_id)
            await s.execute(delete(HedgeEvent).where(HedgeEvent.hedge_position_id.in_(hedge_ids)))
            await s.execute(delete(HedgePosition).where(HedgePosition.account_id == account_id))
            await s.execute(delete(Lot).where(Lot.account_id == account_id))
            await s.execute(delete(Fill).where(Fill.account_id == account_id))
            order_ids = select(Order.id).where(Order.account_id == account_id)
            # Keep source-signal audit history, but detach the orders erased by this reset.
            await s.execute(
                update(CopySignal).where(CopySignal.order_id.in_(order_ids)).values(order_id=None)
            )
            await s.execute(delete(Order).where(Order.account_id == account_id))
            await s.execute(delete(CashReservation).where(CashReservation.account_id == account_id))
            await s.execute(delete(EquitySnapshot).where(EquitySnapshot.account_id == account_id))
            acct.starting_bankroll = starting_bankroll
            acct.cash = starting_bankroll
            acct.reserved = ZERO
            acct.realized_pnl = ZERO
            acct.fees_paid = ZERO
            s.add(
                LedgerEntry(
                    account_id=account_id,
                    kind=EntryKind.DEPOSIT,
                    amount=starting_bankroll,
                    cash_after=starting_bankroll,
                    memo="Paper account reset",
                    created_at=self._clock.now(),
                )
            )

    async def get_account(self, account_id: int) -> Account:
        async with self._sm() as s:
            acct = await s.get(Account, account_id)
            if acct is None:
                raise LedgerError(f"account {account_id} not found")
            return acct

    # -------------------------------------------------------------- reservations
    async def reserve(
        self, account_id: int, amount: Decimal, *, min_reserve: Decimal, key: str, purpose: str
    ) -> Reservation | None:
        """Atomically earmark cash. Returns ``None`` if available cash is insufficient.

        Idempotent on ``key``: a retried call returns the original reservation.
        """
        amount = usdc_down(amount)
        if amount <= ZERO:
            return None
        async with self._sm() as s:
            try:
                async with s.begin():
                    rid = await s.scalar(
                        pg_insert(CashReservation)
                        .values(
                            account_id=account_id,
                            amount=amount,
                            purpose=purpose,
                            status=ReservationStatus.OPEN,
                            idempotency_key=key,
                        )
                        .on_conflict_do_nothing(index_elements=["idempotency_key"])
                        .returning(CashReservation.id)
                    )
                    if rid is None:
                        prior = await s.scalar(
                            select(CashReservation).where(CashReservation.idempotency_key == key)
                        )
                        assert prior is not None
                        return Reservation(prior.id, prior.amount)
                    ok = await s.scalar(
                        update(Account)
                        .where(
                            Account.id == account_id,
                            Account.cash - Account.reserved - min_reserve >= amount,
                        )
                        .values(reserved=Account.reserved + amount)
                        .returning(Account.id)
                    )
                    if ok is None:
                        raise _InsufficientCash
                    return Reservation(rid, amount)
            except _InsufficientCash:
                return None

    async def sync_cash(self, account_id: int, observed: Decimal, memo: str) -> Decimal:
        """Align a LIVE account's cash with the venue balance via an explicit ledger entry.

        The delta is recorded (never silently overwritten) so conservation still holds.
        """
        async with self._sm() as s, s.begin():
            acct = await s.get(Account, account_id, with_for_update=True)
            if acct is None or acct.mode != AccountMode.LIVE:
                raise LedgerError("cash sync is only valid for LIVE accounts")
            delta = observed - acct.cash
            if delta == ZERO:
                return ZERO
            if observed < acct.reserved:
                raise LedgerError("venue balance below reserved cash; refusing to sync")
            acct.cash = observed
            s.add(
                LedgerEntry(
                    account_id=account_id,
                    kind=EntryKind.DEPOSIT,
                    amount=delta,
                    cash_after=observed,
                    memo=memo,
                    created_at=self._clock.now(),
                )
            )
            return delta

    async def release(self, reservation_id: int) -> None:
        async with self._sm() as s, s.begin():
            await self.release_in(s, reservation_id)

    async def release_idle(self, account_id: int) -> int:
        """Release OPEN reservations not backing a PENDING order (emergency stop).

        A signal that reserved but has not inserted its order yet will later fail to book
        ("reservation is not open"), so releasing early can never cause an overspend.
        """
        async with self._sm() as s:
            ids = list(
                (
                    await s.scalars(
                        select(CashReservation.id).where(
                            CashReservation.account_id == account_id,
                            CashReservation.status == ReservationStatus.OPEN,
                            ~select(Order.id)
                            .where(
                                Order.reservation_id == CashReservation.id,
                                Order.status == "PENDING",
                            )
                            .exists(),
                        )
                    )
                ).all()
            )
        for rid in ids:
            await self.release(rid)
        return len(ids)

    async def release_in(self, s: AsyncSession, reservation_id: int) -> Decimal:
        amount = await s.scalar(
            update(CashReservation)
            .where(
                CashReservation.id == reservation_id,
                CashReservation.status == ReservationStatus.OPEN,
            )
            .values(status=ReservationStatus.RELEASED, closed_at=self._clock.now())
            .returning(CashReservation.amount)
        )
        if amount is None:
            return ZERO
        res_account = await s.scalar(
            select(CashReservation.account_id).where(CashReservation.id == reservation_id)
        )
        await s.execute(
            update(Account)
            .where(Account.id == res_account)
            .values(reserved=Account.reserved - amount)
        )
        return amount

    # -------------------------------------------------------------------- fills
    async def book_buy(
        self,
        s: AsyncSession,
        *,
        order: Order,
        fill: FillSimulation,
        origin: LotOrigin,
        outcome: str,
        question: str,
        source_wallet: str | None,
        book_hash: str | None,
        book_received_at: datetime | None,
    ) -> Lot:
        """Book a BUY fill inside the caller's transaction and consume its reservation."""
        if fill.side is not Side.BUY or not fill.filled:
            raise LedgerError("book_buy requires a non-empty BUY fill")
        acct = await s.get(Account, order.account_id, with_for_update=True)
        assert acct is not None
        cost = fill.cash_cost
        if order.reservation_id is not None:
            res = await s.get(CashReservation, order.reservation_id, with_for_update=True)
            if res is None or res.status != ReservationStatus.OPEN:
                raise ReservationError("reservation is not open")
            if cost > res.amount:
                raise ReservationError(f"fill cost {cost} exceeds reservation {res.amount}")
            res.status = ReservationStatus.CONSUMED
            res.closed_at = self._clock.now()
            acct.reserved -= res.amount
        elif cost > acct.cash - acct.reserved:
            raise ReservationError("unreserved fill exceeds available cash")
        acct.cash -= cost
        acct.fees_paid += fill.fee_usdc
        now = self._clock.now()
        f = self._fill_row(order, fill, book_hash, book_received_at, now)
        s.add(f)
        await s.flush()
        lot = Lot(
            account_id=order.account_id,
            condition_id=order.condition_id,
            token_id=order.token_id,
            outcome=outcome,
            market_question=question,
            origin=origin,
            order_id=order.id,
            fill_id=f.id,
            source_wallet=source_wallet,
            entry_price=cost / fill.net_shares,
            shares_acquired=fill.net_shares,
            shares_remaining=fill.net_shares,
            cost_total=cost,
            cost_remaining=cost,
            fee_usdc=fill.fee_usdc,
            matched_shares=ZERO,
            opened_at=now,
        )
        s.add(lot)
        s.add(
            LedgerEntry(
                account_id=order.account_id,
                kind=EntryKind.BUY,
                amount=-cost,
                cash_after=acct.cash,
                fill_id=f.id,
                memo=f"{order.kind} {fill.net_shares} @ {fill.avg_price:.4f}",
                created_at=now,
            )
        )
        await s.flush()
        return lot

    async def book_sell(
        self,
        s: AsyncSession,
        *,
        order: Order,
        fill: FillSimulation,
        allow_matched: bool = False,
        matched_only: bool = False,
        lot_ids: Sequence[int] | None = None,
        book_hash: str | None = None,
        book_received_at: datetime | None = None,
    ) -> list[LotClosure]:
        """Book a SELL fill FIFO across open lots. Never sells more than is held.

        Matched (hedged) shares are untouchable unless ``allow_matched`` or ``matched_only``
        is set by an explicit pair-unwind decision. ``matched_only`` preserves free shares.
        """
        if fill.side is not Side.SELL or not fill.filled:
            raise LedgerError("book_sell requires a non-empty SELL fill")
        acct = await s.get(Account, order.account_id, with_for_update=True)
        assert acct is not None
        q = (
            select(Lot)
            .where(
                Lot.account_id == order.account_id,
                Lot.token_id == order.token_id,
                Lot.shares_remaining > 0,
            )
            .order_by(Lot.opened_at, Lot.id)
            .with_for_update()
        )
        if lot_ids is not None:
            q = q.where(Lot.id.in_(lot_ids))
        lots = list((await s.scalars(q)).all())
        free = {
            lt.id: (
                lt.matched_shares
                if matched_only
                else lt.shares_remaining - (ZERO if allow_matched else lt.matched_shares)
            )
            for lt in lots
        }
        available = sum(free.values(), ZERO)
        if fill.gross_shares > available:
            raise InsufficientInventory(
                f"sell {fill.gross_shares} > available {available} for token {order.token_id}"
            )
        now = self._clock.now()
        f = self._fill_row(order, fill, book_hash, book_received_at, now)
        s.add(f)
        await s.flush()
        proceeds_total = fill.cash_delta
        left = fill.gross_shares
        allocated = ZERO
        closures: list[LotClosure] = []
        realized_total = ZERO
        for lot in lots:
            if left <= ZERO:
                break
            take = min(free[lot.id], left)
            if take <= ZERO:
                continue
            left -= take
            cost = _lot_cost_for(lot, take)
            proceeds = (
                proceeds_total - allocated
                if left <= ZERO
                else usdc_down(proceeds_total * take / fill.gross_shares)
            )
            allocated += proceeds
            if matched_only:
                lot.matched_shares -= take
            elif allow_matched:
                # Consume unmatched shares first; only the excess eats into matched shares.
                unmatched = lot.shares_remaining - lot.matched_shares
                lot.matched_shares -= max(ZERO, take - unmatched)
            lot.shares_remaining -= take
            lot.cost_remaining -= cost
            if lot.shares_remaining == ZERO:
                lot.cost_remaining = ZERO
                lot.closed_at = now
            realized = proceeds - cost
            realized_total += realized
            c = LotClosure(
                lot_id=lot.id,
                kind="SELL",
                fill_id=f.id,
                shares=take,
                proceeds=proceeds,
                cost_released=cost,
                realized_pnl=realized,
                closed_at=now,
            )
            s.add(c)
            closures.append(c)
        acct.cash += proceeds_total
        acct.realized_pnl += realized_total
        acct.fees_paid += fill.fee_usdc
        s.add(
            LedgerEntry(
                account_id=order.account_id,
                kind=EntryKind.SELL,
                amount=proceeds_total,
                cash_after=acct.cash,
                fill_id=f.id,
                memo=f"{order.kind} {fill.gross_shares} @ {fill.avg_price:.4f}",
                created_at=now,
            )
        )
        await s.flush()
        return closures

    def _fill_row(
        self,
        order: Order,
        fill: FillSimulation,
        book_hash: str | None,
        book_received_at: datetime | None,
        now: datetime,
    ) -> Fill:
        assert fill.avg_price is not None and fill.worst_price is not None
        return Fill(
            order_id=order.id,
            account_id=order.account_id,
            side=fill.side.value,
            condition_id=order.condition_id,
            token_id=order.token_id,
            gross_shares=fill.gross_shares,
            net_shares=fill.net_shares,
            notional=fill.notional,
            fee_usdc=fill.fee_usdc,
            cash_delta=fill.cash_delta,
            avg_price=fill.avg_price,
            worst_price=fill.worst_price,
            legs={"legs": [[str(lg.price), str(lg.gross_shares)] for lg in fill.legs]},
            book_hash=book_hash,
            book_received_at=book_received_at,
            executed_at=now,
        )

    # ----------------------------------------------------------------- matching
    async def match_pairs(
        self, s: AsyncSession, *, allocations: Sequence[tuple[int, Decimal]], hedge_lot_id: int
    ) -> Decimal:
        """Mark primary-lot shares and the hedge lot as matched pairs (same condition)."""
        hedge = await s.get(Lot, hedge_lot_id, with_for_update=True)
        assert hedge is not None
        total = ZERO
        for lot_id, shares in allocations:
            primary = await s.get(Lot, lot_id, with_for_update=True)
            if primary is None or primary.condition_id != hedge.condition_id:
                raise LedgerError("hedge must match lots of the same condition")
            if primary.token_id == hedge.token_id:
                raise LedgerError("hedge must be the complementary token")
            free = primary.shares_remaining - primary.matched_shares
            take = min(shares, free)
            if take <= ZERO:
                continue
            primary.matched_shares += take
            total += take
        if total > hedge.shares_remaining - hedge.matched_shares:
            raise LedgerError("hedge lot cannot cover the matched quantity")
        hedge.matched_shares += total
        await s.flush()
        return total

    # --------------------------------------------------------------- settlement
    async def settle_condition(
        self, condition_id: str, payouts: Mapping[str, Decimal], account_id: int, memo: str
    ) -> Decimal:
        """Redeem every open lot on ``condition_id`` at ``payouts[token]`` per share."""
        async with self._sm() as s, s.begin():
            acct = await s.get(Account, account_id, with_for_update=True)
            assert acct is not None
            lots = (
                await s.scalars(
                    select(Lot)
                    .where(
                        Lot.account_id == account_id,
                        Lot.condition_id == condition_id,
                        Lot.shares_remaining > 0,
                    )
                    .with_for_update()
                )
            ).all()
            now = self._clock.now()
            total = ZERO
            realized_total = ZERO
            for lot in lots:
                rate = payouts.get(lot.token_id)
                if rate is None:
                    raise LedgerError(f"missing payout for token {lot.token_id}")
                proceeds = usdc_down(lot.shares_remaining * rate)
                realized = proceeds - lot.cost_remaining
                s.add(
                    LotClosure(
                        lot_id=lot.id,
                        kind="SETTLEMENT",
                        shares=lot.shares_remaining,
                        proceeds=proceeds,
                        cost_released=lot.cost_remaining,
                        realized_pnl=realized,
                        closed_at=now,
                    )
                )
                total += proceeds
                realized_total += realized
                lot.shares_remaining = ZERO
                lot.matched_shares = ZERO
                lot.cost_remaining = ZERO
                lot.closed_at = now
            if not lots:
                return ZERO
            acct.cash += total
            acct.realized_pnl += realized_total
            s.add(
                LedgerEntry(
                    account_id=account_id,
                    kind=EntryKind.SETTLEMENT,
                    amount=total,
                    cash_after=acct.cash,
                    memo=memo,
                    created_at=now,
                )
            )
            return total

    async def merge_matched(self, condition_id: str, account_id: int) -> Decimal:
        """Simulate a CTF merge of all matched pairs on a condition: each pair returns $1."""
        async with self._sm() as s, s.begin():
            return await self.merge_matched_in(s, condition_id, account_id)

    async def merge_matched_in(
        self, s: AsyncSession, condition_id: str, account_id: int
    ) -> Decimal:
        """Merge within the caller's transaction, including any final halt check."""
        acct = await s.get(Account, account_id, with_for_update=True)
        assert acct is not None
        if acct.mode == AccountMode.LIVE:
            raise LedgerError("pair merging is only simulated for PAPER/REPLAY accounts")
        lots = (
            await s.scalars(
                select(Lot)
                .where(
                    Lot.account_id == account_id,
                    Lot.condition_id == condition_id,
                    Lot.matched_shares > 0,
                )
                .order_by(Lot.id)
                .with_for_update()
            )
        ).all()
        by_token: dict[str, list[Lot]] = defaultdict(list)
        for lot in lots:
            by_token[lot.token_id].append(lot)
        if len(by_token) != 2:
            return ZERO
        sides = list(by_token.values())
        pairs = min(sum((lt.matched_shares for lt in side), ZERO) for side in sides)
        if pairs <= ZERO:
            return ZERO
        now = self._clock.now()
        proceeds_total = usdc_down(pairs * ONE)
        # Each pair pays $1; attribute $0.50 per share to each leg so closures sum exactly.
        half = usdc_down(proceeds_total / 2)
        leg_proceeds = {0: half, 1: proceeds_total - half}
        realized_total = ZERO
        for side_idx, side in enumerate(sides):
            left = pairs
            allocated = ZERO
            for lot in side:
                take = min(lot.matched_shares, left)
                if take <= ZERO:
                    continue
                left -= take
                cost = _lot_cost_for(lot, take)
                proceeds = (
                    leg_proceeds[side_idx] - allocated
                    if left <= ZERO
                    else usdc_down(leg_proceeds[side_idx] * take / pairs)
                )
                allocated += proceeds
                lot.matched_shares -= take
                lot.shares_remaining -= take
                lot.cost_remaining -= cost
                if lot.shares_remaining == ZERO:
                    lot.cost_remaining = ZERO
                    lot.closed_at = now
                s.add(
                    LotClosure(
                        lot_id=lot.id,
                        kind="MERGE",
                        shares=take,
                        proceeds=proceeds,
                        cost_released=cost,
                        realized_pnl=proceeds - cost,
                        closed_at=now,
                    )
                )
                realized_total += proceeds - cost
        acct.cash += proceeds_total
        acct.realized_pnl += realized_total
        s.add(
            LedgerEntry(
                account_id=account_id,
                kind=EntryKind.MERGE,
                amount=proceeds_total,
                cash_after=acct.cash,
                memo=f"Merged {pairs} pairs on {condition_id}",
                created_at=now,
            )
        )
        return proceeds_total

    # ------------------------------------------------------------------- views
    async def open_lots(self, account_id: int, condition_id: str | None = None) -> list[Lot]:
        async with self._sm() as s:
            q = select(Lot).where(Lot.account_id == account_id, Lot.shares_remaining > 0)
            if condition_id is not None:
                q = q.where(Lot.condition_id == condition_id)
            return list((await s.scalars(q.order_by(Lot.opened_at, Lot.id))).all())

    async def portfolio(
        self, account_id: int, marks: Mapping[str, Decimal | None]
    ) -> PortfolioView:
        async with self._sm() as s:
            acct = await s.get(Account, account_id)
            if acct is None:
                raise LedgerError(f"account {account_id} not found")
            lots = list(
                (
                    await s.scalars(
                        select(Lot)
                        .where(Lot.account_id == account_id, Lot.shares_remaining > 0)
                        .order_by(Lot.opened_at, Lot.id)
                    )
                ).all()
            )
        return build_portfolio_view(acct, lots, marks, self._clock.now())

    async def day_start_equity(self, account_id: int, day_start: datetime) -> Decimal | None:
        async with self._sm() as s:
            before = await s.scalar(
                select(EquitySnapshot.equity)
                .where(
                    EquitySnapshot.account_id == account_id, EquitySnapshot.taken_at <= day_start
                )
                .order_by(EquitySnapshot.taken_at.desc())
                .limit(1)
            )
            if before is not None:
                return before
            return await s.scalar(
                select(EquitySnapshot.equity)
                .where(
                    EquitySnapshot.account_id == account_id, EquitySnapshot.taken_at >= day_start
                )
                .order_by(EquitySnapshot.taken_at)
                .limit(1)
            )

    async def record_snapshot(self, view: PortfolioView) -> None:
        async with self._sm() as s, s.begin():
            s.add(
                EquitySnapshot(
                    account_id=view.account_id,
                    taken_at=view.as_of,
                    cash=view.cash,
                    reserved=view.reserved,
                    positions_value=view.positions_value,
                    equity=view.equity,
                    realized_pnl=view.realized_pnl,
                    unrealized_pnl=view.unrealized_pnl,
                    locked_profit=view.locked_profit,
                    unmarked_positions=view.unmarked_positions,
                )
            )

    async def verify(self, account_id: int) -> list[str]:
        """Reconcile cached balances against the immutable records. Empty list = consistent."""
        problems: list[str] = []
        async with self._sm() as s:
            acct = await s.get(Account, account_id)
            assert acct is not None
            entries = await s.scalar(
                select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                    LedgerEntry.account_id == account_id
                )
            )
            if (entries or ZERO) != acct.cash:
                problems.append(f"cash {acct.cash} != sum(ledger) {entries}")
            open_res = await s.scalar(
                select(func.coalesce(func.sum(CashReservation.amount), 0)).where(
                    and_(
                        CashReservation.account_id == account_id,
                        CashReservation.status == ReservationStatus.OPEN,
                    )
                )
            )
            if (open_res or ZERO) != acct.reserved:
                problems.append(f"reserved {acct.reserved} != open reservations {open_res}")
            realized = await s.scalar(
                select(func.coalesce(func.sum(LotClosure.realized_pnl), 0))
                .join(Lot, Lot.id == LotClosure.lot_id)
                .where(Lot.account_id == account_id)
            )
            if (realized or ZERO) != acct.realized_pnl:
                problems.append(f"realized {acct.realized_pnl} != closures {realized}")
            lots = (await s.scalars(select(Lot).where(Lot.account_id == account_id))).all()
            closures = (
                await s.execute(
                    select(LotClosure.lot_id, func.sum(LotClosure.shares))
                    .join(Lot, Lot.id == LotClosure.lot_id)
                    .where(Lot.account_id == account_id)
                    .group_by(LotClosure.lot_id)
                )
            ).all()
            closed = {lot_id: Decimal(total) for lot_id, total in closures}
            for lot in lots:
                if lot.shares_acquired - closed.get(lot.id, ZERO) != lot.shares_remaining:
                    problems.append(f"lot {lot.id} share drift")
            matched: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
            for lot in lots:
                matched[lot.condition_id][lot.token_id] += lot.matched_shares
            for cond, per_token in matched.items():
                vals = [v for v in per_token.values() if v > ZERO]
                if vals and (len(vals) != 2 or vals[0] != vals[1]):
                    problems.append(f"unbalanced matched pairs on {cond}: {dict(per_token)}")
        return problems


def build_portfolio_view(
    acct: Account, lots: Sequence[Lot], marks: Mapping[str, Decimal | None], now: datetime
) -> PortfolioView:
    grouped: dict[str, list[Lot]] = defaultdict(list)
    for lot in lots:
        grouped[lot.token_id].append(lot)
    positions: list[PositionView] = []
    unmarked = 0
    for token_id, tlots in grouped.items():
        shares = sum((lt.shares_remaining for lt in tlots), ZERO)
        matched = sum((lt.matched_shares for lt in tlots), ZERO)
        cost = sum((lt.cost_remaining for lt in tlots), ZERO)
        mark = marks.get(token_id)
        if mark is None:
            unmarked += 1
            value = cost
        else:
            value = shares * mark
        positions.append(
            PositionView(
                condition_id=tlots[0].condition_id,
                token_id=token_id,
                outcome=tlots[0].outcome,
                question=tlots[0].market_question,
                shares=shares,
                matched_shares=matched,
                cost_basis=cost,
                avg_entry=cost / shares if shares > ZERO else ZERO,
                mark=mark,
                market_value=value,
                unrealized_pnl=value - cost,
                lot_ids=tuple(lt.id for lt in tlots),
                origins=tuple(sorted({lt.origin for lt in tlots})),
            )
        )
    locked: dict[str, Decimal] = defaultdict(lambda: ZERO)
    pairs: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for lot in lots:
        if lot.matched_shares > ZERO:
            locked[lot.condition_id] -= _lot_cost_for(lot, lot.matched_shares)
            pairs[lot.condition_id] += lot.matched_shares
    for cond in list(locked):
        # matched shares are counted on both legs; each pair pays $1.
        locked[cond] += pairs[cond] / 2
    positions_value = sum((p.market_value for p in positions), ZERO)
    return PortfolioView(
        account_id=acct.id,
        as_of=now,
        starting_bankroll=acct.starting_bankroll,
        cash=acct.cash,
        reserved=acct.reserved,
        positions_value=positions_value,
        realized_pnl=acct.realized_pnl,
        unrealized_pnl=sum((p.unrealized_pnl for p in positions), ZERO),
        locked_profit=sum(locked.values(), ZERO),
        fees_paid=acct.fees_paid,
        positions=tuple(sorted(positions, key=lambda p: (p.condition_id, p.token_id))),
        locked_by_condition=dict(locked),
        unmarked_positions=unmarked,
    )
