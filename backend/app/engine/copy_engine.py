"""CopyEngine: source trade -> strategy -> pre-trade risk -> execution, journaled.

    on_trade(ev)
      screen            strategy: is this a copy candidate at all?
      load context      engine:   all reads in parallel
      decide            strategy: intent (how much) or Skip
      check             risk:     limits, kill switch, limit price; or Rejected
      reserve           ledger:   atomic cash reservation (never overspend)
      execute           venue:    idempotent order, fill booked with the reservation
      record            journal:  one copy_signals row with every reason and the fill

BUY decisions are serialized per condition so concurrent wallets cannot jointly exceed
market caps or ``max_buys_per_market``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.stages import Stage, StageTimer
from app.db.models import (
    CopySignal,
    LotOrigin,
    OrderKind,
    SignalStatus,
    SourceEvent,
    WatchedWallet,
)
from app.domain.market import FeeModel, Side
from app.domain.reasons import Reason
from app.domain.settings import StrategySettings
from app.engine.context import ContextLoader, source_trade
from app.engine.journal import SignalJournal
from app.execution.orders import ExecutionRequest, ExecutionResult, Executor
from app.risk.pretrade import check_buy, check_sell, halt_reason
from app.services.event_bus import EventBus
from app.services.ledger import PortfolioLedger
from app.services.settings_service import SettingsService
from app.strategy.copy import PAIR_UNWIND_REVIEW, Skip, decide_buy, decide_sell, screen

log = logging.getLogger(__name__)


class CopyEngine:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        journal: SignalJournal,
        contexts: ContextLoader,
        ledger: PortfolioLedger,
        executor: Executor,
        settings: SettingsService,
        bus: EventBus,
        clock: Clock,
        account_id: int,
    ) -> None:
        self._sm = sm
        self._journal = journal
        self._contexts = contexts
        self._ledger = ledger
        self._exec = executor
        self._settings = settings
        self._bus = bus
        self._clock = clock
        self.account_id = account_id
        self._market_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def on_trade(
        self, ev: SourceEvent, wallet: WatchedWallet, timer: StageTimer | None = None
    ) -> CopySignal | None:
        """Decide and execute the copy of one ingested source event. Idempotent.

        ``timer`` carries marks from fill receipt; without one (restart recovery, replay)
        stages are timed from this call.
        """
        if ev.type != "TRADE" or ev.side not in ("BUY", "SELL") or ev.is_backfill:
            return None  # non-trades and pre-follow history are recorded, never copied
        timer = timer or StageTimer()
        sig = await self._journal.open(ev)
        if sig.status != SignalStatus.PENDING:
            return sig
        settings = await self._settings.get(refresh=True)
        followed = wallet.removed_at is None and wallet.follow
        skip = screen(source_trade(ev), followed=followed, settings=settings)
        if skip is not None:
            return await self._journal.finish(sig, SignalStatus.SKIPPED, [skip], timer=timer)
        try:
            if ev.side == "BUY":
                async with self._market_locks[ev.condition_id]:
                    return await self._buy(sig, ev, wallet, settings, timer)
            return await self._sell(sig, ev, wallet, settings, timer)
        except Exception as exc:
            log.exception("copy signal failed", extra={"signal_id": sig.id})
            return await self._journal.finish(
                sig,
                SignalStatus.FAILED,
                [Reason("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")],
                timer=timer,
            )

    async def _buy(
        self,
        sig: CopySignal,
        ev: SourceEvent,
        wallet: WatchedWallet,
        settings: StrategySettings,
        timer: StageTimer,
    ) -> CopySignal:
        timer.mark(Stage.STARTED)
        inputs = await self._contexts.buy(ev, wallet, settings)
        timer.mark(Stage.CONTEXT)
        intent = decide_buy(inputs.strategy)
        if isinstance(intent, Skip):
            timer.mark(Stage.DECIDED)
            return await self._finish_skip(sig, intent, timer)
        trade = inputs.strategy.trade
        risk = check_buy(
            budget=intent.budget,
            token_id=trade.token_id,
            condition_id=trade.condition_id,
            source_price=trade.price,
            source_ts=trade.block_ts,
            now=self._clock.now(),
            book=inputs.book,
            portfolio=inputs.snapshot,
            settings=settings.risk,
            trading_halted=settings.trading_halted,
            buys_in_market=inputs.buys_in_market,
        )
        timer.mark(Stage.DECIDED)
        reasons = [*intent.reasons, *risk.reasons]
        record = {
            **intent.record,
            "approved_budget": risk.approved_budget,
            "limit_price": risk.limit_price,
        }
        if risk.rejected:
            return await self._journal.finish(
                sig, SignalStatus.REJECTED, reasons, timer=timer, **record
            )
        reservation = await self._ledger.reserve(
            self.account_id,
            risk.approved_budget,
            min_reserve=settings.risk.min_cash_reserve_usd,
            key=f"sig-{sig.id}",
            purpose="COPY_BUY",
        )
        timer.mark(Stage.RESERVED)
        if reservation is None:
            short = Reason(
                "INSUFFICIENT_CASH", "Available unreserved cash is below the approved budget."
            )
            return await self._journal.finish(
                sig, SignalStatus.REJECTED, [*reasons, short], timer=timer, **record
            )
        decided_at = self._clock.now()
        sig = await self._journal.save(sig, decided_at=decided_at, **record)
        result = await self._execute(
            ExecutionRequest(
                account_id=self.account_id,
                idempotency_key=f"copy-{sig.id}",
                kind=OrderKind.COPY_BUY,
                side=Side.BUY,
                condition_id=trade.condition_id,
                token_id=trade.token_id,
                fee_model=FeeModel(
                    schedule=intent.market.fee_schedule, buy_fee_mode=settings.paper.buy_fee_mode
                ),
                max_book_age=timedelta(seconds=settings.risk.max_book_age_s),
                budget=risk.approved_budget,
                limit_price=risk.limit_price,
                reservation_id=reservation.id,
                origin=LotOrigin.COPY,
                outcome=intent.token.outcome,
                question=intent.market.question,
                source_wallet=wallet.address,
                signal_id=sig.id,
                min_order_usd=settings.risk.min_order_usd,
            ),
            settings,
        )
        timer.mark(Stage.EXECUTED)
        return await self._journal.record_execution(sig, result, reasons, decided_at, timer)

    async def _sell(
        self,
        sig: CopySignal,
        ev: SourceEvent,
        wallet: WatchedWallet,
        settings: StrategySettings,
        timer: StageTimer,
    ) -> CopySignal:
        timer.mark(Stage.STARTED)
        halted = halt_reason(settings)
        if halted is not None:
            timer.mark(Stage.DECIDED)
            return await self._journal.finish(sig, SignalStatus.REJECTED, [halted], timer=timer)
        inputs = await self._contexts.sell(ev, wallet)
        timer.mark(Stage.CONTEXT)
        intent = decide_sell(inputs.strategy, settings)
        if any(r.code == PAIR_UNWIND_REVIEW for r in intent.reasons):
            self._bus.publish(
                "pair_unwind_review", {"condition_id": ev.condition_id, "token_id": ev.token_id}
            )
        if isinstance(intent, Skip):
            timer.mark(Stage.DECIDED)
            return await self._finish_skip(sig, intent, timer)
        gate = check_sell(inputs.book, settings)
        timer.mark(Stage.DECIDED)
        if gate.rejection is not None:
            return await self._journal.finish(
                sig,
                SignalStatus.REJECTED,
                [*intent.reasons, gate.rejection],
                timer=timer,
                **intent.record,
            )
        decided_at = self._clock.now()
        sig = await self._journal.save(
            sig, decided_at=decided_at, limit_price=gate.limit_price, **intent.record
        )
        market = inputs.market
        result = await self._execute(
            ExecutionRequest(
                account_id=self.account_id,
                idempotency_key=f"copy-{sig.id}",
                kind=OrderKind.COPY_SELL,
                side=Side.SELL,
                condition_id=ev.condition_id,
                token_id=ev.token_id,
                fee_model=FeeModel(
                    schedule=market.fee_schedule if market else FeeModel().schedule,
                    buy_fee_mode=settings.paper.buy_fee_mode,
                ),
                max_book_age=timedelta(seconds=settings.risk.max_book_age_s),
                target_shares=intent.shares,
                limit_price=gate.limit_price,
                lot_ids=intent.lot_ids,
                source_wallet=wallet.address,
                signal_id=sig.id,
            ),
            settings,
        )
        timer.mark(Stage.EXECUTED)
        return await self._journal.record_execution(
            sig, result, [*intent.reasons], decided_at, timer
        )

    async def _finish_skip(self, sig: CopySignal, skip: Skip, timer: StageTimer) -> CopySignal:
        return await self._journal.finish(
            sig, skip.status, [*skip.reasons], timer=timer, **skip.record
        )

    async def _execute(self, req: ExecutionRequest, settings: StrategySettings) -> ExecutionResult:
        return await self._exec.execute(
            req,
            delay_ms=settings.paper.execution_delay_ms,
            slippage_bps=settings.paper.extra_slippage_bps,
            share_step=settings.paper.share_step,
        )

    async def resume_unfinished(self) -> int:
        """Restart recovery: finish signals left PENDING and events never turned into signals."""
        async with self._sm() as s:
            pending = (
                await s.execute(
                    select(SourceEvent, WatchedWallet)
                    .join(WatchedWallet, WatchedWallet.id == SourceEvent.wallet_id)
                    .outerjoin(CopySignal, CopySignal.source_event_id == SourceEvent.id)
                    .where(
                        SourceEvent.type == "TRADE",
                        SourceEvent.is_backfill.is_(False),
                        (CopySignal.id.is_(None)) | (CopySignal.status == SignalStatus.PENDING),
                    )
                    .order_by(SourceEvent.block_ts, SourceEvent.id)
                )
            ).all()
        for ev, wallet in pending:
            await self.on_trade(ev, wallet)
        return len(pending)
