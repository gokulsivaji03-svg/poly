"""HedgeScannerService: finds, (optionally) executes and reconciles complementary hedges.

Coordinates with copy trading through the shared PortfolioLedger:

* primaries are COPY lots with unmatched shares; quotes walk the complement's asks;
* a hedge BUY books its lot *and* the pair matching in one transaction;
* matched pairs are protected from copy sells (ledger) and only unwound under the explicit
  ``UNWIND_IF_BETTER`` policy when both legs' executable bids beat the locked $1 payout;
* settlement is booked only when on-chain payouts are observed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.money import ONE, ZERO
from app.db.models import (
    Account,
    AccountMode,
    HedgeEvent,
    HedgePosition,
    Lot,
    LotOrigin,
    Order,
    OrderKind,
    OrderStatus,
)
from app.domain.fills import simulate_sell
from app.domain.hedge import HedgeLot, HedgeQuote, quote_hedge
from app.domain.hedge_state import (
    HedgeEvent as Ev,
)
from app.domain.hedge_state import (
    HedgeState,
    InvalidHedgeTransition,
    transition,
)
from app.domain.market import FeeModel, MarketInfo, Side
from app.domain.settings import HedgedSellPolicy, HedgeMode, StrategySettings
from app.execution.orders import ExecutionRequest, ExecutionResult, Executor
from app.execution.paper import apply_slippage
from app.risk.pretrade import halt_reason
from app.services.event_bus import EventBus
from app.services.ledger import LedgerError, PortfolioLedger
from app.services.market_data import MarketDataService
from app.services.settings_service import SettingsService
from app.services.system_events import SystemEventLog

log = logging.getLogger(__name__)


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


def opp_id(condition_id: str, primary_token_id: str) -> str:
    return f"{condition_id}:{primary_token_id}"


@dataclass
class Opportunity:
    position_id: int
    market: MarketInfo | None
    quote: HedgeQuote
    state: HedgeState
    existing_shares: Decimal
    existing_entry: Decimal

    def to_dict(self) -> dict[str, Any]:
        q = self.quote
        m = self.market
        primary = m.token(q.primary_token_id) if m else None
        hedge = m.token(q.hedge_token_id) if m and q.hedge_token_id else None
        book_age = None
        if q.book_received_at is not None and q.quoted_at is not None:
            book_age = int((q.quoted_at - q.book_received_at).total_seconds() * 1000)
        return {
            "id": opp_id(q.condition_id, q.primary_token_id),
            "condition_id": q.condition_id,
            "question": m.question if m else "",
            "primary_token_id": q.primary_token_id,
            "primary_outcome": primary.outcome if primary else "",
            "hedge_token_id": q.hedge_token_id,
            "hedge_outcome": hedge.outcome if hedge else "",
            "state": self.state.value,
            "viable": q.viable,
            "reasons": list(q.reasons),
            "existing_shares": _s(self.existing_shares),
            "unhedged_shares": _s(q.total_unhedged),
            "existing_entry_price": _s(self.existing_entry),
            "hedge_best_ask": _s(q.best_ask),
            "hedge_avg_price": _s(q.avg_hedge_price),
            "hedge_worst_price": _s(q.fill.worst_price if q.fill else None),
            "combined_cost_per_share": _s(q.combined_cost_per_share),
            "matched_shares": _s(q.matched_shares),
            "net_profit_per_share": _s(q.net_per_share),
            "net_profit": _s(q.net_profit),
            "gross_profit": _s(q.gross_profit),
            "primary_fees": _s(q.primary_fees),
            "hedge_fees": _s(q.hedge_fees),
            "capital_required": _s(q.capital_required),
            "expected_return": _s(q.return_on_capital),
            "depth_usd": _s(q.depth_usd),
            "quoted_at": q.quoted_at.isoformat() if q.quoted_at else None,
            "book_age_ms": book_age,
        }


class HedgeScannerService:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        ledger: PortfolioLedger,
        executor: Executor,
        market_data: MarketDataService,
        settings: SettingsService,
        events: SystemEventLog,
        bus: EventBus,
        clock: Clock,
        account_id: int,
    ) -> None:
        self._sm = sm
        self._ledger = ledger
        self._exec = executor
        self._md = market_data
        self._settings = settings
        self._events = events
        self._bus = bus
        self._clock = clock
        self.account_id = account_id
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.opportunities: dict[str, Opportunity] = {}
        self.last_scan_at: Any = None
        self.scan_duration_ms: int | None = None
        self.positions_scanned = 0

    # ------------------------------------------------------------------ state
    async def _position(
        self, s: AsyncSession, condition_id: str, primary: str, hedge_token: str
    ) -> HedgePosition:
        now = self._clock.now()
        await s.execute(
            pg_insert(HedgePosition)
            .values(
                account_id=self.account_id,
                condition_id=condition_id,
                primary_token_id=primary,
                hedge_token_id=hedge_token,
                state=HedgeState.UNHEDGED,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["account_id", "condition_id", "primary_token_id"]
            )
        )
        pos = await s.scalar(
            select(HedgePosition)
            .where(
                HedgePosition.account_id == self.account_id,
                HedgePosition.condition_id == condition_id,
                HedgePosition.primary_token_id == primary,
            )
            .with_for_update()
        )
        assert pos is not None
        return pos

    def _apply(
        self, s: AsyncSession, pos: HedgePosition, event: Ev, detail: dict[str, Any] | None = None
    ) -> None:
        old = HedgeState(pos.state)
        try:
            new = transition(old, event)
            kind = event.value
        except InvalidHedgeTransition:
            new = self._derive(pos, event)
            kind = "STATE_RESYNC"
        if (
            new is old
            and kind != "STATE_RESYNC"
            and event in (Ev.OPPORTUNITY_FOUND, Ev.OPPORTUNITY_LOST, Ev.INVENTORY_CHANGED_UNHEDGED)
        ):
            return  # no-op refresh; avoid flooding the event log
        pos.state = new.value
        pos.updated_at = self._clock.now()
        s.add(
            HedgeEvent(
                hedge_position_id=pos.id,
                kind=kind,
                from_state=old.value,
                to_state=new.value,
                detail=detail or {"event": event.value},
                created_at=self._clock.now(),
            )
        )

    @staticmethod
    def _derive(pos: HedgePosition, event: Ev) -> HedgeState:
        if pos.state == HedgeState.SETTLED:
            return HedgeState.SETTLED  # terminal: a late scan event must not reopen it
        mapping = {
            Ev.FILL_RECONCILED_FULL: HedgeState.FULLY_HEDGED,
            Ev.FILL_RECONCILED_PARTIAL: HedgeState.PARTIALLY_HEDGED,
            Ev.ORDER_FAILED: HedgeState.HEDGE_FAILED,
            Ev.OPPORTUNITY_FOUND: HedgeState.HEDGE_OPPORTUNITY,
            Ev.OPPORTUNITY_LOST: HedgeState.UNHEDGED,
            Ev.ORDER_SUBMITTED: HedgeState.HEDGE_PENDING,
            Ev.INVENTORY_CHANGED_UNHEDGED: HedgeState.PARTIALLY_HEDGED,
        }
        return mapping.get(event, HedgeState(pos.state))

    def states(self) -> dict[tuple[str, str], str]:
        return {
            (o.quote.condition_id, o.quote.primary_token_id): o.state.value
            for o in self.opportunities.values()
        }

    # ------------------------------------------------------------------- scan
    async def _primaries(self) -> dict[tuple[str, str], list[Lot]]:
        lots = await self._ledger.open_lots(self.account_id)
        groups: dict[tuple[str, str], list[Lot]] = defaultdict(list)
        for lt in lots:
            if lt.origin == LotOrigin.COPY:
                groups[(lt.condition_id, lt.token_id)].append(lt)
        return groups

    async def scan(self) -> list[Opportunity]:
        started = time.monotonic()
        settings = await self._settings.get(refresh=True)
        groups = await self._primaries()
        seen: set[str] = set()
        for (cond, token), lots in groups.items():
            async with self._locks[cond]:
                opp = await self._scan_one(cond, token, lots, settings)
            if opp is None:
                continue
            key = opp_id(cond, token)
            seen.add(key)
            self.opportunities[key] = opp
            if (
                settings.hedge.enabled
                and settings.hedge.mode is HedgeMode.AUTO_PAPER
                and opp.quote.viable
                and halt_reason(settings) is None
            ):
                await self.execute(key, manual=False)
        for key in set(self.opportunities) - seen:
            self.opportunities.pop(key, None)
        self.positions_scanned = len(groups)
        self.last_scan_at = self._clock.now()
        self.scan_duration_ms = int((time.monotonic() - started) * 1000)
        self._bus.publish("hedge", {})
        return list(self.opportunities.values())

    async def _scan_one(
        self, cond: str, token: str, lots: list[Lot], settings: StrategySettings
    ) -> Opportunity | None:
        market = await self._md.get_market(cond)
        hedge_token = ""
        if market is not None and (comp := market.complement(token)) is not None:
            hedge_token = comp.token_id
        book = await self._md.get_book(hedge_token) if hedge_token else None
        hedge_lots = [
            HedgeLot(
                lot_id=lt.id,
                token_id=lt.token_id,
                unhedged_shares=lt.shares_remaining - lt.matched_shares,
                cost_per_share=lt.cost_remaining / lt.shares_remaining,
                fee_per_share=lt.fee_usdc / lt.shares_acquired,
            )
            for lt in lots
            if lt.shares_remaining > ZERO
        ]
        now = self._clock.now()
        if market is None:
            quote = HedgeQuote(
                cond,
                token,
                hedge_token,
                False,
                ("MARKET_UNAVAILABLE",),
                total_unhedged=sum((h.unhedged_shares for h in hedge_lots), ZERO),
                quoted_at=now,
            )
        else:
            quote = quote_hedge(
                market=market,
                primary_token_id=token,
                lots=hedge_lots,
                book=book,
                fees=FeeModel(
                    schedule=market.fee_schedule, buy_fee_mode=settings.paper.buy_fee_mode
                ),
                settings=settings.hedge,
                now=now,
                max_book_age=timedelta(seconds=settings.risk.max_book_age_s),
                share_step=settings.paper.share_step,
            )
        shares = sum((lt.shares_remaining for lt in lots), ZERO)
        matched = sum((lt.matched_shares for lt in lots), ZERO)
        cost = sum((lt.cost_remaining for lt in lots), ZERO)
        async with self._sm() as s, s.begin():
            pos = await self._position(s, cond, token, hedge_token)
            state = HedgeState(pos.state)
            if state is not HedgeState.HEDGE_PENDING and state is not HedgeState.SETTLED:
                unmatched = shares - matched
                if matched > ZERO and unmatched <= ZERO and state is not HedgeState.FULLY_HEDGED:
                    self._apply(s, pos, Ev.FILL_RECONCILED_FULL, {"resync": True})
                elif state is HedgeState.FULLY_HEDGED and unmatched > ZERO:
                    self._apply(s, pos, Ev.INVENTORY_CHANGED_UNHEDGED)
                elif matched > ZERO and state in (
                    HedgeState.UNHEDGED,
                    HedgeState.HEDGE_FAILED,
                    HedgeState.HEDGE_OPPORTUNITY,
                ):
                    self._apply(s, pos, Ev.FILL_RECONCILED_PARTIAL, {"resync": True})
                elif quote.viable:
                    self._apply(
                        s,
                        pos,
                        Ev.OPPORTUNITY_FOUND,
                        {
                            "net_profit": _s(quote.net_profit),
                            "matched_shares": _s(quote.matched_shares),
                        },
                    )
                elif state in (HedgeState.HEDGE_OPPORTUNITY, HedgeState.HEDGE_FAILED):
                    self._apply(s, pos, Ev.OPPORTUNITY_LOST, {"reasons": list(quote.reasons)})
            opp = Opportunity(
                pos.id,
                market,
                quote,
                HedgeState(pos.state),
                shares,
                cost / shares if shares > ZERO else ZERO,
            )
            pos.last_quote = opp.to_dict()
            pos.hedge_token_id = hedge_token
        return opp

    # ---------------------------------------------------------------- execute
    async def execute(self, key: str, *, manual: bool = True) -> dict[str, Any]:
        cond, _, token = key.partition(":")
        async with self._locks[cond]:
            settings = await self._settings.get(refresh=True)
            halted = halt_reason(settings)
            if halted is not None:
                return {
                    "status": "REJECTED",
                    "order_id": None,
                    "matched_shares": "0",
                    "reason": halted.code,
                }
            if not settings.hedge.enabled:
                return {
                    "status": "REJECTED",
                    "order_id": None,
                    "matched_shares": "0",
                    "reason": "HEDGING_DISABLED",
                }
            lots = (await self._primaries()).get((cond, token), [])
            opp = await self._scan_one(cond, token, lots, settings)
            if opp is None or not opp.quote.viable or opp.quote.fill is None:
                reasons = ",".join(opp.quote.reasons) if opp else "NO_POSITION"
                return {
                    "status": "REJECTED",
                    "order_id": None,
                    "matched_shares": "0",
                    "reason": reasons or "NOT_VIABLE",
                }
            outcome = await self._execute_quote(opp, settings, manual=manual)
            # Refresh the cached view from the ledger so the UI never shows a stale state.
            lots = (await self._primaries()).get((cond, token), [])
            if lots:
                refreshed = await self._scan_one(cond, token, lots, settings)
                if refreshed is not None:
                    self.opportunities[key] = refreshed
            else:
                self.opportunities.pop(key, None)
            self._bus.publish("hedge", {})
            return outcome

    async def _execute_quote(
        self, opp: Opportunity, settings: StrategySettings, *, manual: bool
    ) -> dict[str, Any]:
        q = opp.quote
        assert q.fill is not None and opp.market is not None
        async with self._sm() as s, s.begin():
            pos = await self._position(s, q.condition_id, q.primary_token_id, q.hedge_token_id)
            if pos.state == HedgeState.HEDGE_PENDING:
                return {
                    "status": "REJECTED",
                    "order_id": None,
                    "matched_shares": "0",
                    "reason": "HEDGE_ALREADY_PENDING",
                }
            self._apply(
                s, pos, Ev.ORDER_SUBMITTED, {"manual": manual, "quote_net": _s(q.net_profit)}
            )
            attempt = self._clock.now().isoformat()
        # Small buffer covers fee rounding; the limit price bounds the true cost anyway.
        budget = q.fill.cash_cost * Decimal("1.01")
        reservation = await self._ledger.reserve(
            self.account_id,
            budget,
            min_reserve=settings.risk.min_cash_reserve_usd,
            key=f"hedge-{pos.id}-{attempt}",
            purpose="HEDGE_BUY",
        )
        if reservation is None:
            return await self._after(opp, None, "INSUFFICIENT_CASH")
        hedge_token = opp.market.token(q.hedge_token_id)
        result = await self._exec.execute(
            ExecutionRequest(
                account_id=self.account_id,
                idempotency_key=f"hedge-{pos.id}-{attempt}",
                kind=OrderKind.HEDGE_BUY,
                side=Side.BUY,
                condition_id=q.condition_id,
                token_id=q.hedge_token_id,
                fee_model=FeeModel(
                    schedule=opp.market.fee_schedule, buy_fee_mode=settings.paper.buy_fee_mode
                ),
                max_book_age=timedelta(seconds=settings.risk.max_book_age_s),
                budget=reservation.amount,
                target_shares=q.matched_shares,
                limit_price=q.fill.worst_price,
                reservation_id=reservation.id,
                origin=LotOrigin.HEDGE,
                outcome=hedge_token.outcome if hedge_token else "",
                question=opp.market.question,
                hedge_id=pos.id,
                match_allocations=tuple((a.lot_id, a.shares) for a in q.allocations),
            ),
            delay_ms=settings.paper.execution_delay_ms,
            slippage_bps=settings.paper.extra_slippage_bps,
            share_step=settings.paper.share_step,
        )
        return await self._after(opp, result, None)

    async def _after(
        self, opp: Opportunity, result: ExecutionResult | None, error: str | None
    ) -> dict[str, Any]:
        q = opp.quote
        lots = (await self._primaries()).get((q.condition_id, q.primary_token_id), [])
        matched = sum((lt.matched_shares for lt in lots), ZERO)
        unmatched = sum((lt.shares_remaining for lt in lots), ZERO) - matched
        async with self._sm() as s, s.begin():
            pos = await self._position(s, q.condition_id, q.primary_token_id, q.hedge_token_id)
            if result is not None and result.filled:
                ev = Ev.FILL_RECONCILED_FULL if unmatched <= ZERO else Ev.FILL_RECONCILED_PARTIAL
                assert result.fill is not None
                self._apply(
                    s,
                    pos,
                    ev,
                    {
                        "order_id": result.order_id,
                        "delivered": _s(result.fill.net_shares),
                        "cost": _s(result.fill.cash_cost),
                        "fill_status": result.status.value,
                        "matched_total": _s(matched),
                        "unmatched": _s(unmatched),
                    },
                )
                pos.last_error = None
            else:
                reason = error or (result.reason if result else "UNKNOWN")
                self._apply(s, pos, Ev.ORDER_FAILED, {"reason": reason})
                pos.last_error = reason
            state = pos.state
        self._bus.publish("hedge", {})
        self._bus.publish("portfolio", {})
        self._bus.publish("positions", {})
        if result is not None and result.filled:
            assert result.fill is not None
            await self._events.record(
                "HEDGE_FILLED",
                f"Hedged {result.fill.net_shares} of {q.condition_id[:10]}… ({state})",
                data={"order_id": result.order_id},
            )
        return {
            "status": result.status.value if result else "REJECTED",
            "order_id": result.order_id if result else None,
            "matched_shares": _s(result.fill.net_shares) if result and result.fill else "0",
            "reason": None
            if result and result.status is OrderStatus.FILLED
            else (error or (result.reason if result else None)),
            "state": state,
        }

    # ------------------------------------------------------ unwind / settlement
    async def review_unwind(self, condition_id: str) -> str:
        """Under UNWIND_IF_BETTER, sell matched pairs only if both legs' bids beat $1/pair."""
        async with self._locks[condition_id]:
            settings = await self._settings.get(refresh=True)
            if (halted := halt_reason(settings)) is not None:
                return halted.code
            if settings.copy_.hedged_sell_policy is not HedgedSellPolicy.UNWIND_IF_BETTER:
                return "POLICY_PRESERVE"
            if (await self._ledger.get_account(self.account_id)).mode == AccountMode.LIVE:
                return "PAPER_ONLY"
            lots = await self._ledger.open_lots(self.account_id, condition_id)
            by_token: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for lt in lots:
                by_token[lt.token_id] += lt.matched_shares
            if len(by_token) != 2 or min(by_token.values()) <= ZERO:
                return "NO_MATCHED_PAIRS"
            pairs = min(by_token.values())
            market = await self._md.get_market(condition_id)
            if market is None:
                return "MARKET_UNAVAILABLE"
            fees = FeeModel(schedule=market.fee_schedule)
            proceeds = ZERO
            legs = []
            max_age = timedelta(seconds=settings.risk.max_book_age_s)
            for token in by_token:
                book = await self._md.get_book(token, fresh=True)
                if book is None:
                    return "NO_BOOK"
                if book.token_id != token:
                    return "BOOK_TOKEN_MISMATCH"
                if book.is_stale(self._clock.now(), max_age):
                    return "STALE_BOOK"
                sim = simulate_sell(
                    apply_slippage(book, settings.paper.extra_slippage_bps),
                    fees,
                    shares=pairs,
                    share_step=settings.paper.share_step,
                )
                if sim.gross_shares < pairs:
                    await self._events.record(
                        "UNWIND_SKIPPED",
                        "Insufficient bid depth to unwind",
                        data={"condition_id": condition_id},
                    )
                    return "INSUFFICIENT_DEPTH"
                if sim.gross_shares < max(book.min_order_size, market.min_order_size):
                    return "BELOW_MIN_ORDER_SIZE"
                legs.append((book, sim))
                proceeds += sim.cash_delta
            locked_value = pairs * ONE
            if proceeds <= locked_value:
                await self._events.record(
                    "UNWIND_SKIPPED",
                    f"Unwind proceeds {proceeds:.4f} <= locked payout {locked_value:.4f}; "
                    "holding pairs to settlement",
                    data={"condition_id": condition_id},
                )
                return "HOLD_BETTER"
            # Both paper fills use the reviewed books and commit together. Sequential venue
            # executions could fill only one leg, leaving the formerly locked pair exposed.
            try:
                async with self._sm() as s, s.begin():
                    acct = await s.get(Account, self.account_id, with_for_update=True)
                    if acct is None or acct.mode == AccountMode.LIVE:
                        return "PAPER_ONLY"
                    if halted := halt_reason(await self._settings.get(refresh=True)):
                        return halted.code
                    now = self._clock.now()
                    if any(book.is_stale(now, max_age) for book, _ in legs):
                        return "STALE_BOOK"
                    for book, sim in legs:
                        order = Order(
                            account_id=self.account_id,
                            idempotency_key=f"unwind-{self.account_id}-{book.token_id}-{now.isoformat()}",
                            mode=acct.mode,
                            kind=OrderKind.PAIR_UNWIND,
                            side=Side.SELL.value,
                            condition_id=condition_id,
                            token_id=book.token_id,
                            requested_shares=pairs,
                            status=OrderStatus.FILLED,
                            created_at=now,
                            completed_at=now,
                            book_age_ms=int(book.age(now).total_seconds() * 1000),
                            exec_latency_ms=0,
                        )
                        s.add(order)
                        await s.flush()
                        await self._ledger.book_sell(
                            s,
                            order=order,
                            fill=sim,
                            matched_only=True,
                            book_hash=book.hash,
                            book_received_at=book.received_at,
                        )
            except LedgerError as exc:
                await self._events.record(
                    "UNWIND_SKIPPED",
                    f"Matched inventory changed before paired unwind: {exc}",
                    data={"condition_id": condition_id},
                )
                return "INVENTORY_CHANGED"
            await self._events.record(
                "PAIR_UNWOUND", f"Unwound {pairs} pairs on {condition_id}", level="WARNING"
            )
            self._bus.publish("portfolio", {})
            self._bus.publish("positions", {})
            return "UNWOUND"

    async def settle_resolved(self) -> int:
        """Book settlement for conditions whose on-chain payouts are observed."""
        lots = await self._ledger.open_lots(self.account_id)
        settled = 0
        for cond in sorted({lt.condition_id for lt in lots}):
            market = await self._md.get_market(cond, refresh=True)
            if market is None or not market.resolved:
                continue
            tokens = {lt.token_id for lt in lots if lt.condition_id == cond}
            if not tokens <= set(market.payouts):
                await self._events.record(
                    "RESOLUTION_PAYOUT_UNKNOWN",
                    f"{cond} resolved but payouts not observable yet",
                    level="WARNING",
                )
                continue
            async with self._locks[cond]:
                paid = await self._ledger.settle_condition(
                    cond, market.payouts, self.account_id, f"Settlement of {market.question}"
                )
                async with self._sm() as s, s.begin():
                    rows = (
                        await s.scalars(
                            select(HedgePosition)
                            .where(
                                HedgePosition.account_id == self.account_id,
                                HedgePosition.condition_id == cond,
                            )
                            .with_for_update()
                        )
                    ).all()
                    for pos in rows:
                        self._apply(s, pos, Ev.SETTLED, {"proceeds": _s(paid)})
            settled += 1
            await self._events.record("MARKET_SETTLED", f"Settled {cond}: {paid}")
            self._bus.publish("portfolio", {})
        return settled

    async def merge(self, key: str) -> dict[str, Any]:
        """Simulate a CTF merge of matched pairs (paper). Not instantaneous on-chain."""
        cond, _, _ = key.partition(":")
        async with self._locks[cond], self._sm() as s, s.begin():
            acct = await s.get(Account, self.account_id, with_for_update=True)
            if acct is None or acct.mode == AccountMode.LIVE:
                return {"proceeds": "0", "reason": "PAPER_ONLY"}
            if halted := halt_reason(await self._settings.get(refresh=True)):
                return {"proceeds": "0", "reason": halted.code}
            proceeds = await self._ledger.merge_matched_in(s, cond, self.account_id)
        if proceeds > ZERO:
            await self._events.record("PAIRS_MERGED", f"Simulated merge on {cond}: {proceeds}")
            self._bus.publish("portfolio", {})
            self._bus.publish("positions", {})
        return {"proceeds": _s(proceeds)}

    # ------------------------------------------------------------------ views
    async def payload(self) -> dict[str, Any]:
        opps = sorted(
            self.opportunities.values(),
            key=lambda o: (not o.quote.viable, -(o.quote.net_profit or ZERO)),
        )
        settings = await self._settings.get()
        return {
            "scanner": {
                "enabled": settings.hedge.enabled,
                "mode": settings.hedge.mode.value,
                "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
                "scan_duration_ms": self.scan_duration_ms,
                "positions_scanned": self.positions_scanned,
            },
            "opportunities": [o.to_dict() for o in opps],
        }

    async def recover(self) -> int:
        """Startup: a HEDGE_PENDING left by a crash is failed; the next scan resyncs facts."""
        async with self._sm() as s, s.begin():
            rows = (
                await s.scalars(
                    select(HedgePosition)
                    .where(
                        HedgePosition.account_id == self.account_id,
                        HedgePosition.state == HedgeState.HEDGE_PENDING,
                    )
                    .with_for_update()
                )
            ).all()
            for pos in rows:
                self._apply(s, pos, Ev.ORDER_FAILED, {"reason": "RECOVERED_AFTER_RESTART"})
        return len(rows)

    async def events_list(self, limit: int) -> list[dict[str, Any]]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(HedgeEvent, HedgePosition)
                    .join(HedgePosition, HedgePosition.id == HedgeEvent.hedge_position_id)
                    .where(HedgePosition.account_id == self.account_id)
                    .order_by(HedgeEvent.id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "id": e.id,
                "condition_id": p.condition_id,
                "primary_token_id": p.primary_token_id,
                "kind": e.kind,
                "from_state": e.from_state,
                "to_state": e.to_state,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e, p in rows
        ]

    async def run(self, stop: asyncio.Event) -> None:
        """Scanner loop; also reacts to pair-unwind reviews requested by copy sells."""
        sub = self._bus.subscribe()
        try:
            while not stop.is_set():
                settings = await self._settings.get(refresh=True)
                if settings.hedge.enabled:
                    await self.scan()
                deadline = time.monotonic() + settings.hedge.scan_interval_s
                while not stop.is_set() and time.monotonic() < deadline:
                    try:
                        msg = await asyncio.wait_for(
                            sub.queue.get(), timeout=max(0.1, deadline - time.monotonic())
                        )
                    except TimeoutError:
                        break
                    if msg["type"] == "pair_unwind_review":
                        with contextlib.suppress(Exception):
                            await self.review_unwind(msg["data"]["condition_id"])
        finally:
            self._bus.unsubscribe(sub)
