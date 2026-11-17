"""CopySignalService: source trade -> sizing -> risk -> reservation -> paper execution.

Every source TRADE gets exactly one ``copy_signals`` row (unique per source event) that
records the inputs, every reduction/skip reason, and the actual simulated fill.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.money import ONE, ZERO
from app.db.models import (
    CopySignal,
    Lot,
    LotOrigin,
    Order,
    OrderKind,
    OrderStatus,
    SignalStatus,
    SourceEvent,
    WatchedWallet,
)
from app.domain.market import FeeModel, Side
from app.domain.risk import Reason, evaluate_buy
from app.domain.sell_policy import plan_copy_sell
from app.domain.settings import SizingSettings, StrategySettings
from app.domain.sizing import size_copy
from app.services.equity_estimator import WalletEquityEstimator
from app.services.event_bus import EventBus
from app.services.ledger import PortfolioLedger, PortfolioView
from app.services.market_data import MarketDataService
from app.services.paper_execution import ExecutionRequest, PaperExecutionEngine
from app.services.settings_service import SettingsService, deep_merge

log = logging.getLogger(__name__)


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


def effective_sizing(base: SizingSettings, override: dict[str, Any] | None) -> SizingSettings:
    if not override:
        return base
    return SizingSettings.model_validate(deep_merge(base.model_dump(mode="json"), override))


class CopySignalService:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        ledger: PortfolioLedger,
        executor: PaperExecutionEngine,
        market_data: MarketDataService,
        estimator: WalletEquityEstimator,
        settings: SettingsService,
        bus: EventBus,
        clock: Clock,
        account_id: int,
    ) -> None:
        self._sm = sm
        self._ledger = ledger
        self._exec = executor
        self._md = market_data
        self._est = estimator
        self._settings = settings
        self._bus = bus
        self._clock = clock
        self.account_id = account_id
        # Per-condition: count/exposure checks through reservation and fill must not interleave
        # across wallets, or max_buys_per_market and market caps can be exceeded.
        self._market_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --------------------------------------------------------------- portfolio
    async def portfolio(self) -> PortfolioView:
        lots = await self._ledger.open_lots(self.account_id)
        marks = await self._md.marks(lt.token_id for lt in lots)
        return await self._ledger.portfolio(self.account_id, {t: m.price for t, m in marks.items()})

    async def day_pnl(self, view: PortfolioView) -> Decimal:
        now = self._clock.now()
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        start_equity = await self._ledger.day_start_equity(self.account_id, day_start)
        return ZERO if start_equity is None else view.equity - start_equity

    # ----------------------------------------------------------------- signals
    async def _open_signal(self, ev: SourceEvent) -> CopySignal:
        async with self._sm() as s, s.begin():
            await s.execute(
                pg_insert(CopySignal)
                .values(
                    source_event_id=ev.id,
                    wallet_id=ev.wallet_id,
                    status=SignalStatus.PENDING,
                    side=ev.side or "",
                    condition_id=ev.condition_id,
                    token_id=ev.token_id,
                    source_price=ev.price,
                    source_shares=ev.size,
                    source_cost=ev.usdc_size if ev.usdc_size > ZERO else ev.size * ev.price,
                    reasons={"items": []},
                    detected_at=ev.detected_at,
                    detection_lag_ms=_ms(ev.block_ts, ev.detected_at),
                )
                .on_conflict_do_nothing(index_elements=["source_event_id"])
            )
            sig = await s.scalar(select(CopySignal).where(CopySignal.source_event_id == ev.id))
            assert sig is not None
        return sig

    async def _save(self, sig: CopySignal, **fields: Any) -> CopySignal:
        async with self._sm() as s, s.begin():
            row = await s.get(CopySignal, sig.id, with_for_update=True)
            assert row is not None
            for k, v in fields.items():
                setattr(row, k, v)
        self._bus.publish("copy_signal", {"id": row.id})
        return row

    async def _finish(
        self, sig: CopySignal, status: SignalStatus, reasons: list[Reason], **fields: Any
    ) -> CopySignal:
        decided_at = fields.pop("decided_at", None) or self._clock.now()
        return await self._save(
            sig,
            status=status,
            decided_at=decided_at,
            signal_latency_ms=_ms(sig.detected_at, decided_at),
            reasons={"items": [{"code": r.code, "message": r.message} for r in reasons]},
            **fields,
        )

    async def process(self, ev: SourceEvent, wallet: WatchedWallet) -> CopySignal | None:
        """Decide and (paper-)execute the copy for one ingested source event. Idempotent."""
        if ev.type != "TRADE" or ev.side not in ("BUY", "SELL"):
            return None  # transfers, redemptions, splits, merges are recorded, never copied
        if ev.is_backfill:
            return None  # predates following the wallet: history/metrics only, never copied
        sig = await self._open_signal(ev)
        if sig.status != SignalStatus.PENDING:
            return sig
        settings = await self._settings.get(refresh=True)
        skip = self._policy_skip(ev, wallet, settings)
        if skip is not None:
            return await self._finish(sig, SignalStatus.SKIPPED, [skip])
        try:
            if ev.side == "BUY":
                async with self._market_locks[ev.condition_id]:
                    return await self._buy(sig, ev, wallet, settings)
            return await self._sell(sig, ev, wallet, settings)
        except Exception as exc:
            log.exception("copy signal failed", extra={"signal_id": sig.id})
            return await self._finish(
                sig, SignalStatus.FAILED, [Reason("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")]
            )

    @staticmethod
    def _policy_skip(
        ev: SourceEvent, wallet: WatchedWallet, settings: StrategySettings
    ) -> Reason | None:
        if ev.is_combo:
            return Reason("COMBO_UNSUPPORTED", "Combo/parlay trades are not copied.")
        if wallet.removed_at is not None or not wallet.follow:
            return Reason("NOT_FOLLOWED", "Wallet is monitored but copying is off.")
        if not settings.copy_.enabled:
            return Reason("COPY_DISABLED", "Copy trading is disabled in settings.")
        if ev.side == "SELL" and not settings.copy_.copy_sells:
            return Reason("SELL_COPY_DISABLED", "Copying source sells is disabled.")
        if not ev.condition_id or not ev.token_id or not (ZERO < ev.price < ONE):
            return Reason("UNMAPPED_EVENT", "Event lacks market/token mapping or a valid price.")
        return None

    async def _sold_after(self, ev: SourceEvent) -> int | None:
        async with self._sm() as s:
            return await s.scalar(
                select(SourceEvent.id).where(
                    SourceEvent.wallet_id == ev.wallet_id,
                    SourceEvent.token_id == ev.token_id,
                    SourceEvent.type == "TRADE",
                    SourceEvent.side == "SELL",
                    SourceEvent.block_ts > ev.block_ts,
                ).limit(1)
            )

    async def _view_with_day_pnl(self) -> tuple[PortfolioView, Decimal]:
        view = await self.portfolio()
        return view, await self.day_pnl(view)

    async def _buys_in_market(self, condition_id: str) -> int:
        async with self._sm() as s:
            n = await s.scalar(
                select(func.count(Order.id)).where(
                    Order.account_id == self.account_id,
                    Order.kind == OrderKind.COPY_BUY,
                    Order.condition_id == condition_id,
                    Order.status.in_([OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]),
                )
            )
        return n or 0

    async def _buy(
        self, sig: CopySignal, ev: SourceEvent, wallet: WatchedWallet, settings: StrategySettings
    ) -> CopySignal:
        reasons: list[Reason] = []
        # Independent reads run concurrently; the checks below keep their original order.
        sold_after, market, (view, day_pnl), estimate, book, buys_in_market = await asyncio.gather(
            self._sold_after(ev),
            self._md.get_market(ev.condition_id),
            self._view_with_day_pnl(),
            self._est.estimate_at(
                ev.wallet_id, wallet.address, ev.block_ts, exclude_event_id=ev.id
            ),
            self._md.get_book(ev.token_id),
            self._buys_in_market(ev.condition_id),
        )
        if sold_after is not None:
            # Late-indexed BUY: its later SELL was already handled, so a copy could never exit.
            return await self._finish(
                sig,
                SignalStatus.SKIPPED,
                [Reason("SOURCE_SOLD_AFTER", "Source already sold this token after this buy.")],
            )
        if market is None:
            return await self._finish(
                sig,
                SignalStatus.REJECTED,
                [Reason("MARKET_UNAVAILABLE", "Market metadata unavailable.")],
            )
        token = market.token(ev.token_id)
        if token is None:
            return await self._finish(
                sig,
                SignalStatus.REJECTED,
                [Reason("TOKEN_MISMATCH", "Traded token is not an outcome of this condition.")],
            )
        if market.closed or not market.accepting_orders:
            return await self._finish(
                sig,
                SignalStatus.REJECTED,
                [Reason("MARKET_CLOSED", "Market is not accepting orders.")],
            )
        if not market.fees_known:
            reasons.append(Reason("FEES_ASSUMED", "Fee schedule unavailable; assumed 7% rate."))

        sizing = size_copy(
            source_trade_cost=sig.source_cost,
            source_equity=estimate,
            user_equity=view.equity,
            settings=effective_sizing(settings.sizing, wallet.sizing_override),
        )
        reasons.extend(Reason("SIZING_NOTE", n) for n in sizing.notes)
        common: dict[str, Any] = {
            "source_equity": sizing.source_equity_used or estimate.equity,
            "source_equity_coverage": estimate.coverage.value,
            "source_confidence": estimate.confidence,
            "source_alloc_pct": sizing.source_allocation_pct,
            "user_equity": view.equity,
            "proposed_pct": sizing.proposed_copy_pct,
            "target_budget": sizing.target_budget,
        }
        if sizing.skipped:
            return await self._finish(
                sig,
                SignalStatus.SKIPPED,
                [*reasons, Reason(sizing.skip_code or "SIZING_SKIP", "; ".join(sizing.notes))],
                **common,
            )
        decision = evaluate_buy(
            budget=sizing.target_budget,
            token_id=ev.token_id,
            condition_id=ev.condition_id,
            source_price=ev.price,
            source_ts=ev.block_ts,
            now=self._clock.now(),
            book=book,
            portfolio=view.risk_snapshot(day_pnl),
            settings=settings.risk,
            trading_halted=settings.trading_halted,
            buys_in_market=buys_in_market or 0,
        )
        reasons.extend(decision.reasons)
        common.update(approved_budget=decision.approved_budget, limit_price=decision.limit_price)
        if decision.rejected:
            return await self._finish(sig, SignalStatus.REJECTED, reasons, **common)

        reservation = await self._ledger.reserve(
            self.account_id,
            decision.approved_budget,
            min_reserve=settings.risk.min_cash_reserve_usd,
            key=f"sig-{sig.id}",
            purpose="COPY_BUY",
        )
        if reservation is None:
            return await self._finish(
                sig,
                SignalStatus.REJECTED,
                [
                    *reasons,
                    Reason(
                        "INSUFFICIENT_CASH",
                        "Available unreserved cash is below the approved budget.",
                    ),
                ],
                **common,
            )
        decided_at = self._clock.now()
        sig = await self._save(sig, decided_at=decided_at, **common)
        result = await self._exec.execute(
            ExecutionRequest(
                account_id=self.account_id,
                idempotency_key=f"copy-{sig.id}",
                kind=OrderKind.COPY_BUY,
                side=Side.BUY,
                condition_id=ev.condition_id,
                token_id=ev.token_id,
                fee_model=FeeModel(
                    schedule=market.fee_schedule, buy_fee_mode=settings.paper.buy_fee_mode
                ),
                max_book_age=timedelta(seconds=settings.risk.max_book_age_s),
                budget=decision.approved_budget,
                limit_price=decision.limit_price,
                reservation_id=reservation.id,
                origin=LotOrigin.COPY,
                outcome=token.outcome,
                question=market.question,
                source_wallet=wallet.address,
                signal_id=sig.id,
                min_order_usd=settings.risk.min_order_usd,
            ),
            delay_ms=settings.paper.execution_delay_ms,
            slippage_bps=settings.paper.extra_slippage_bps,
            share_step=settings.paper.share_step,
        )
        return await self._record_result(sig, result, reasons, decided_at)

    async def _record_result(
        self, sig: CopySignal, result: Any, reasons: list[Reason], decided_at: datetime
    ) -> CopySignal:
        status_map = {
            OrderStatus.FILLED: SignalStatus.EXECUTED,
            OrderStatus.PARTIALLY_FILLED: SignalStatus.PARTIAL,
            OrderStatus.REJECTED: SignalStatus.REJECTED,
            OrderStatus.FAILED: SignalStatus.FAILED,
        }
        status = status_map.get(result.status, SignalStatus.FAILED)
        if result.status is OrderStatus.PARTIALLY_FILLED:
            reasons.append(
                Reason("PARTIAL_FILL", "Book depth within limit exhausted; remainder cancelled.")
            )
        if result.reason and not result.filled:
            reasons.append(
                Reason("EXECUTION_" + result.reason.split()[0].split(":")[0], result.reason)
            )
        fill = result.fill
        exec_ms = None
        async with self._sm() as s:
            from app.db.models import Order

            order = await s.get(Order, result.order_id)
            if order is not None:
                exec_ms = order.exec_latency_ms
        if result.filled:
            self._bus.publish("portfolio", {})
            self._bus.publish("positions", {})
        return await self._finish(
            sig,
            status,
            reasons,
            decided_at=decided_at,
            order_id=result.order_id,
            executed_at=self._clock.now() if result.filled else None,
            filled_shares=fill.net_shares if fill and result.filled else None,
            filled_cost=fill.cash_cost if fill and result.filled else None,
            avg_fill_price=fill.avg_price if fill and result.filled else None,
            exec_latency_ms=exec_ms,
        )

    async def _sell(
        self, sig: CopySignal, ev: SourceEvent, wallet: WatchedWallet, settings: StrategySettings
    ) -> CopySignal:
        if settings.trading_halted:
            return await self._finish(
                sig,
                SignalStatus.REJECTED,
                [Reason("TRADING_HALTED", "Strategies are paused or emergency-stopped.")],
            )
        async with self._sm() as s:
            lots = list(
                (
                    await s.scalars(
                        select(Lot)
                        .where(
                            Lot.account_id == self.account_id,
                            Lot.token_id == ev.token_id,
                            Lot.source_wallet == wallet.address,
                            Lot.origin == LotOrigin.COPY,
                            Lot.shares_remaining > 0,
                        )
                        .order_by(Lot.opened_at, Lot.id)
                    )
                ).all()
            )
        total = sum((lt.shares_remaining for lt in lots), ZERO)
        matched = sum((lt.matched_shares for lt in lots), ZERO)
        after = await self._est.source_position_size(wallet, ev.token_id)
        before = None if after is None else after + ev.size
        plan = plan_copy_sell(
            source_sell_shares=ev.size,
            source_position_before=before,
            our_total_shares=total,
            our_matched_shares=matched,
            policy=settings.copy_.sell_policy,
            hedged_policy=settings.copy_.hedged_sell_policy,
            share_step=settings.paper.share_step,
        )
        reasons = [Reason(code, code.replace("_", " ").capitalize()) for code in plan.reasons]
        common: dict[str, Any] = {
            "target_shares": plan.shares_to_sell,
            "source_alloc_pct": plan.source_sell_fraction,
        }
        if plan.consider_pair_unwind:
            reasons.append(
                Reason(
                    "PAIR_UNWIND_REVIEW",
                    "Hedge engine will evaluate unwinding matched pairs (UNWIND_IF_BETTER).",
                )
            )
            self._bus.publish(
                "pair_unwind_review", {"condition_id": ev.condition_id, "token_id": ev.token_id}
            )
        if plan.shares_to_sell <= ZERO:
            return await self._finish(
                sig,
                SignalStatus.SKIPPED,
                reasons or [Reason("NOTHING_TO_SELL", "No unhedged copied inventory to reduce.")],
                **common,
            )
        book = await self._md.get_book(ev.token_id)
        limit = None
        if book is not None and book.best_bid is not None:
            limit = book.best_bid * (ONE - settings.risk.max_slippage_pct)
        market = await self._md.get_market(ev.condition_id)
        decided_at = self._clock.now()
        sig = await self._save(sig, decided_at=decided_at, limit_price=limit, **common)
        result = await self._exec.execute(
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
                target_shares=plan.shares_to_sell,
                limit_price=limit,
                lot_ids=tuple(lt.id for lt in lots),
                source_wallet=wallet.address,
                signal_id=sig.id,
            ),
            delay_ms=settings.paper.execution_delay_ms,
            slippage_bps=settings.paper.extra_slippage_bps,
            share_step=settings.paper.share_step,
        )
        return await self._record_result(sig, result, reasons, decided_at)

    async def resume_unfinished(self) -> int:
        """Restart recovery: finish signals left PENDING and events never turned into signals."""
        async with self._sm() as s:
            pending = list(
                (
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
            )
        for ev, wallet in pending:
            await self.process(ev, wallet)
        return len(pending)
