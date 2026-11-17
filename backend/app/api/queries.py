"""Read models for the API: ORM rows -> JSON-ready dicts (decimals as strings)."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.core.money import ZERO, hours_between
from app.db.models import (
    CopySignal,
    EquitySnapshot,
    Fill,
    Lot,
    Order,
    SignalStatus,
    SourceEvent,
    SystemEvent,
    WalletEquitySnapshot,
    WalletPosition,
    WatchedWallet,
)
from app.domain.equity import EquityCoverage
from app.runtime import Runtime
from app.services.equity_estimator import BASE_CONFIDENCE, _decay
from app.services.ledger import PortfolioView


def s(v: Any) -> str | None:
    """Decimal -> plain string without exponent or trailing zeros; ``None`` stays ``None``."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def ts(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def pct(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == ZERO:
        return None
    return a / b


# ------------------------------------------------------------------ portfolio
def portfolio_summary(view: PortfolioView, day_pnl: Decimal) -> dict[str, Any]:
    return {
        "account_id": view.account_id,
        "as_of": ts(view.as_of),
        "starting_bankroll": s(view.starting_bankroll),
        "equity": s(view.equity),
        "cash": s(view.cash),
        "reserved": s(view.reserved),
        "available_cash": s(view.available_cash),
        "invested": s(view.invested),
        "positions_value": s(view.positions_value),
        "realized_pnl": s(view.realized_pnl),
        "unrealized_pnl": s(view.unrealized_pnl),
        "locked_profit": s(view.locked_profit),
        "fees_paid": s(view.fees_paid),
        "unmarked_positions": view.unmarked_positions,
        "day_pnl": s(day_pnl),
    }


async def portfolio(rt: Runtime) -> dict[str, Any]:
    view = await rt.portfolio.view()
    return portfolio_summary(view, await rt.portfolio.day_pnl(view))


async def dashboard(rt: Runtime) -> dict[str, Any]:
    settings = await rt.settings.get()
    now = rt.clock.now()
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    async with rt.sm() as sess:
        followed = await sess.scalar(
            select(func.count())
            .select_from(WatchedWallet)
            .where(WatchedWallet.removed_at.is_(None), WatchedWallet.follow.is_(True))
        )
        copied = await sess.scalar(
            select(func.count())
            .select_from(CopySignal)
            .where(
                CopySignal.status.in_([SignalStatus.EXECUTED, SignalStatus.PARTIAL]),
                CopySignal.executed_at >= day_start,
            )
        )
    return {
        "portfolio": await portfolio(rt),
        "followed_wallets": followed or 0,
        "trades_copied_today": copied or 0,
        "mode": rt.cfg.execution_mode.value,
        "strategies_paused": settings.strategies_paused,
        "emergency_stop": settings.emergency_stop,
        "copy_enabled": settings.copy_.enabled,
        "hedge_enabled": settings.hedge.enabled,
        "hedge_mode": settings.hedge.mode.value,
    }


async def equity_history(rt: Runtime, range_: str) -> dict[str, Any]:
    spans = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}
    q = select(EquitySnapshot).where(EquitySnapshot.account_id == rt.account_id)
    if range_ in spans:
        q = q.where(EquitySnapshot.taken_at >= rt.clock.now() - spans[range_])
    async with rt.sm() as sess:
        rows = (await sess.scalars(q.order_by(EquitySnapshot.taken_at).limit(5000))).all()
    return {
        "points": [
            {
                "t": ts(r.taken_at),
                "equity": s(r.equity),
                "cash": s(r.cash),
                "positions_value": s(r.positions_value),
                "realized_pnl": s(r.realized_pnl),
                "unrealized_pnl": s(r.unrealized_pnl),
                "locked_profit": s(r.locked_profit),
            }
            for r in rows
        ]
    }


def hedge_state_from_quantities(shares: Decimal, matched: Decimal) -> str:
    if matched > ZERO and matched >= shares:
        return "FULLY_HEDGED"
    if matched > ZERO:
        return "PARTIALLY_HEDGED"
    return "UNHEDGED"


async def positions(
    rt: Runtime, hedge_states: dict[tuple[str, str], str] | None = None
) -> dict[str, Any]:
    lots = await rt.ledger.open_lots(rt.account_id)
    marks = await rt.market_data.marks(lt.token_id for lt in lots)
    view = await rt.ledger.portfolio(rt.account_id, {t: m.price for t, m in marks.items()})
    by_token: dict[str, list[Lot]] = {}
    for lt in lots:
        by_token.setdefault(lt.token_id, []).append(lt)
    out = []
    for p in view.positions:
        market = await rt.market_data.get_market(p.condition_id)
        comp = market.complement(p.token_id) if market else None
        mark = marks.get(p.token_id)
        state = (hedge_states or {}).get((p.condition_id, p.token_id)) or (
            hedge_state_from_quantities(p.shares, p.matched_shares)
        )
        out.append(
            {
                "condition_id": p.condition_id,
                "token_id": p.token_id,
                "opposite_token_id": comp.token_id if comp else None,
                "question": p.question or (market.question if market else ""),
                "outcome": p.outcome,
                "shares": s(p.shares),
                "matched_shares": s(p.matched_shares),
                "unmatched_shares": s(p.unmatched_shares),
                "hedged_pct": s(p.hedged_pct),
                "avg_entry": s(p.avg_entry),
                "cost_basis": s(p.cost_basis),
                "mark": s(p.mark),
                "executable_bid": s(mark.best_bid) if mark else None,
                "market_value": s(p.market_value),
                "unrealized_pnl": s(p.unrealized_pnl),
                "locked_profit": s(view.locked_by_condition.get(p.condition_id, ZERO)),
                "hedge_state": state,
                "origins": list(p.origins),
                "lots": [lot_dict(lt) for lt in by_token.get(p.token_id, [])],
            }
        )
    return {"positions": out}


def lot_dict(lt: Lot) -> dict[str, Any]:
    return {
        "id": lt.id,
        "origin": lt.origin,
        "source_wallet": lt.source_wallet,
        "entry_price": s(lt.entry_price),
        "shares_acquired": s(lt.shares_acquired),
        "shares_remaining": s(lt.shares_remaining),
        "matched_shares": s(lt.matched_shares),
        "cost_remaining": s(lt.cost_remaining),
        "fee_usdc": s(lt.fee_usdc),
        "opened_at": ts(lt.opened_at),
    }


async def orders(rt: Runtime, limit: int) -> dict[str, Any]:
    async with rt.sm() as sess:
        rows = (
            await sess.scalars(
                select(Order)
                .where(Order.account_id == rt.account_id)
                .order_by(Order.id.desc())
                .limit(limit)
            )
        ).all()
    out = []
    for o in rows:
        f: Fill | None = o.fills[0] if o.fills else None
        out.append(
            {
                "id": o.id,
                "kind": o.kind,
                "side": o.side,
                "status": o.status,
                "token_id": o.token_id,
                "condition_id": o.condition_id,
                "requested_budget": s(o.requested_budget),
                "requested_shares": s(o.requested_shares),
                "limit_price": s(o.limit_price),
                "reason": o.reason,
                "created_at": ts(o.created_at),
                "completed_at": ts(o.completed_at),
                "exec_latency_ms": o.exec_latency_ms,
                "book_age_ms": o.book_age_ms,
                "fill": None
                if f is None
                else {
                    "gross_shares": s(f.gross_shares),
                    "net_shares": s(f.net_shares),
                    "notional": s(f.notional),
                    "fee_usdc": s(f.fee_usdc),
                    "cash_delta": s(f.cash_delta),
                    "avg_price": s(f.avg_price),
                    "worst_price": s(f.worst_price),
                },
            }
        )
    return {"orders": out}


# ---------------------------------------------------------------------- feeds
def source_trade_dict(ev: SourceEvent, w: WatchedWallet, sig: CopySignal | None) -> dict[str, Any]:
    return {
        "id": ev.id,
        "wallet_id": w.id,
        "wallet_address": w.address,
        "wallet_nickname": w.nickname,
        "type": ev.type,
        "side": ev.side,
        "condition_id": ev.condition_id,
        "token_id": ev.token_id,
        "outcome": ev.outcome,
        "title": ev.title,
        "slug": ev.slug,
        "size": s(ev.size),
        "usdc_size": s(ev.usdc_size),
        "price": s(ev.price),
        "tx_hash": ev.tx_hash,
        "block_ts": ts(ev.block_ts),
        "detected_at": ts(ev.detected_at),
        "detection_lag_ms": int((ev.detected_at - ev.block_ts).total_seconds() * 1000),
        "is_backfill": ev.is_backfill,
        "signal_id": sig.id if sig else None,
        "signal_status": sig.status if sig else None,
    }


async def source_trades(
    rt: Runtime,
    *,
    limit: int,
    wallet_id: int | None = None,
    before_id: int | None = None,
    event_id: int | None = None,
) -> list[dict[str, Any]]:
    q = (
        select(SourceEvent, WatchedWallet, CopySignal)
        .join(WatchedWallet, WatchedWallet.id == SourceEvent.wallet_id)
        .outerjoin(CopySignal, CopySignal.source_event_id == SourceEvent.id)
    )
    if event_id is not None:
        q = q.where(SourceEvent.id == event_id)
    if wallet_id is not None:
        q = q.where(SourceEvent.wallet_id == wallet_id)
    else:
        q = q.where(SourceEvent.is_backfill.is_(False))
    if before_id is not None:
        q = q.where(SourceEvent.id < before_id)
    q = q.order_by(SourceEvent.block_ts.desc(), SourceEvent.id.desc()).limit(limit)
    async with rt.sm() as sess:
        rows = (await sess.execute(q)).all()
    return [source_trade_dict(ev, w, sig) for ev, w, sig in rows]


def signal_dict(sig: CopySignal, w: WatchedWallet, ev: SourceEvent) -> dict[str, Any]:
    diff = None
    if sig.avg_fill_price is not None and sig.source_price:
        diff = (sig.avg_fill_price - sig.source_price) / sig.source_price * Decimal(10_000)
        if sig.side == "SELL":
            diff = -diff
    return {
        "id": sig.id,
        "source_event_id": sig.source_event_id,
        "wallet_id": w.id,
        "wallet_nickname": w.nickname,
        "wallet_address": w.address,
        "status": sig.status,
        "side": sig.side,
        "title": ev.title,
        "outcome": ev.outcome,
        "token_id": sig.token_id,
        "condition_id": sig.condition_id,
        "source_price": s(sig.source_price),
        "source_shares": s(sig.source_shares),
        "source_cost": s(sig.source_cost),
        "source_equity": s(sig.source_equity),
        "source_equity_coverage": sig.source_equity_coverage,
        "source_confidence": s(sig.source_confidence),
        "source_alloc_pct": s(sig.source_alloc_pct),
        "user_equity": s(sig.user_equity),
        "proposed_pct": s(sig.proposed_pct),
        "target_budget": s(sig.target_budget),
        "approved_budget": s(sig.approved_budget),
        "limit_price": s(sig.limit_price),
        "target_shares": s(sig.target_shares),
        "filled_shares": s(sig.filled_shares),
        "filled_cost": s(sig.filled_cost),
        "avg_fill_price": s(sig.avg_fill_price),
        "price_diff_bps": s(diff.quantize(Decimal("0.1"))) if diff is not None else None,
        "reasons": list(sig.reasons.get("items", [])),
        "detected_at": ts(sig.detected_at),
        "decided_at": ts(sig.decided_at),
        "executed_at": ts(sig.executed_at),
        "detection_lag_ms": sig.detection_lag_ms,
        "signal_latency_ms": sig.signal_latency_ms,
        "exec_latency_ms": sig.exec_latency_ms,
        "order_id": sig.order_id,
    }


async def copy_signals(
    rt: Runtime, *, limit: int, signal_id: int | None = None
) -> list[dict[str, Any]]:
    q = (
        select(CopySignal, WatchedWallet, SourceEvent)
        .join(WatchedWallet, WatchedWallet.id == CopySignal.wallet_id)
        .join(SourceEvent, SourceEvent.id == CopySignal.source_event_id)
    )
    if signal_id is not None:
        q = q.where(CopySignal.id == signal_id)
    async with rt.sm() as sess:
        rows = (await sess.execute(q.order_by(CopySignal.id.desc()).limit(limit))).all()
    return [signal_dict(sig, w, ev) for sig, w, ev in rows]


# -------------------------------------------------------------------- wallets
async def wallet_summary(rt: Runtime, w: WatchedWallet) -> dict[str, Any]:
    now = rt.clock.now()
    async with rt.sm() as sess:
        snap = await sess.scalar(
            select(WalletEquitySnapshot)
            .where(WalletEquitySnapshot.wallet_id == w.id)
            .order_by(WalletEquitySnapshot.observed_at.desc())
            .limit(1)
        )
        lags = (
            await sess.scalars(
                select(SourceEvent)
                .where(SourceEvent.wallet_id == w.id, SourceEvent.is_backfill.is_(False))
                .order_by(SourceEvent.id.desc())
                .limit(50)
            )
        ).all()
        trades_7d = await sess.scalar(
            select(func.count())
            .select_from(SourceEvent)
            .where(
                SourceEvent.wallet_id == w.id,
                SourceEvent.type == "TRADE",
                SourceEvent.block_ts >= now - timedelta(days=7),
            )
        )
    coverage = EquityCoverage(snap.coverage) if snap else EquityCoverage.UNAVAILABLE
    equity = None
    if snap is not None and snap.positions_value is not None:
        equity = snap.positions_value + (snap.cash_balance or ZERO)
    confidence = ZERO
    if snap is not None:
        hours = hours_between(snap.observed_at, now)
        confidence = (BASE_CONFIDENCE[coverage] * _decay(hours)).quantize(Decimal("0.01"))
    delays = [int((e.detected_at - e.block_ts).total_seconds() * 1000) for e in lags]
    return {
        "id": w.id,
        "address": w.address,
        "nickname": w.nickname,
        "follow": w.follow,
        "added_at": ts(w.added_at),
        "sizing_override": w.sizing_override,
        "estimated_equity": s(equity),
        "equity_coverage": coverage.value,
        "equity_is_lower_bound": coverage is EquityCoverage.POSITIONS_ONLY,
        "equity_confidence": s(confidence),
        "equity_as_of": ts(snap.observed_at) if snap else None,
        "positions_value": s(snap.positions_value) if snap else None,
        "cash_balance": s(snap.cash_balance) if snap else None,
        "open_positions": snap.open_positions if snap else None,
        "observed_realized_pnl": s(snap.realized_pnl) if snap else None,
        "observed_unrealized_pnl": s(snap.unrealized_pnl) if snap else None,
        "last_trade_at": ts(w.last_trade_at),
        "last_success_at": ts(w.last_success_at),
        "last_error": w.last_error,
        "freshness_s": int((now - w.last_success_at).total_seconds())
        if w.last_success_at
        else None,
        "ingestion_delay_ms": int(statistics.median(delays)) if delays else None,
        "trades_7d": trades_7d or 0,
    }


async def wallet_detail(rt: Runtime, w: WatchedWallet) -> dict[str, Any]:
    summary = await wallet_summary(rt, w)
    ev_alias = aliased(SourceEvent)
    async with rt.sm() as sess:
        trades = (
            await sess.scalars(
                select(SourceEvent)
                .where(SourceEvent.wallet_id == w.id, SourceEvent.type == "TRADE")
                .order_by(SourceEvent.block_ts)
            )
        ).all()
        signals = (
            await sess.execute(
                select(CopySignal, ev_alias.block_ts)
                .join(ev_alias, ev_alias.id == CopySignal.source_event_id)
                .where(CopySignal.wallet_id == w.id)
                .order_by(ev_alias.block_ts)
            )
        ).all()
        snaps = (
            await sess.scalars(
                select(WalletEquitySnapshot)
                .where(WalletEquitySnapshot.wallet_id == w.id)
                .order_by(WalletEquitySnapshot.observed_at)
                .limit(2000)
            )
        ).all()
        pos = (
            await sess.scalars(
                select(WalletPosition)
                .where(WalletPosition.wallet_id == w.id)
                .order_by(WalletPosition.current_value.desc().nulls_last())
            )
        ).all()
    buys = [t for t in trades if t.side == "BUY"]
    span_days = None
    if len(trades) >= 2:
        span_days = hours_between(trades[0].block_ts, trades[-1].block_ts) / Decimal(24)
    sigs = [sg for sg, _ in signals]
    copied = [sg for sg in sigs if sg.status in (SignalStatus.EXECUTED, SignalStatus.PARTIAL)]
    skipped = [sg for sg in sigs if sg.status in (SignalStatus.SKIPPED, SignalStatus.REJECTED)]
    reasons: Counter[str] = Counter()
    for sg in skipped:
        items = sg.reasons.get("items", [])
        if items:
            reasons[items[-1]["code"]] += 1
    diffs = [
        (sg.avg_fill_price - sg.source_price) / sg.source_price * Decimal(10_000)
        for sg in copied
        if sg.avg_fill_price is not None and sg.side == "BUY"
    ]
    lags = [sg.detection_lag_ms for sg in sigs if sg.detection_lag_ms is not None]
    position_sizes = [p.current_value for p in pos if p.current_value is not None]
    summary.update(
        {
            "metrics": {
                "trade_count": len(trades),
                "buy_count": len(buys),
                "sell_count": len(trades) - len(buys),
                "trades_per_day": s((Decimal(len(trades)) / span_days).quantize(Decimal("0.01")))
                if span_days and span_days > ZERO
                else None,
                "avg_trade_usd": s(
                    (sum((t.usdc_size for t in trades), ZERO) / len(trades)).quantize(
                        Decimal("0.01")
                    )
                )
                if trades
                else None,
                "avg_position_usd": s(
                    (sum(position_sizes, ZERO) / len(position_sizes)).quantize(Decimal("0.01"))
                )
                if position_sizes
                else None,
                "copied_count": len(copied),
                "skipped_count": len(skipped),
                "copy_rate": s((Decimal(len(copied)) / len(sigs)).quantize(Decimal("0.0001")))
                if sigs
                else None,
                "avg_price_diff_bps": s((sum(diffs, ZERO) / len(diffs)).quantize(Decimal("0.1")))
                if diffs
                else None,
                "median_detection_lag_ms": int(statistics.median(lags)) if lags else None,
                "skip_reasons": dict(reasons),
            },
            "equity_history": [
                {
                    "t": ts(sn.observed_at),
                    "equity": s(sn.positions_value + (sn.cash_balance or ZERO))
                    if sn.positions_value is not None
                    else None,
                    "positions_value": s(sn.positions_value),
                    "cash_balance": s(sn.cash_balance),
                    "coverage": sn.coverage,
                    "confidence": s(BASE_CONFIDENCE[EquityCoverage(sn.coverage)]),
                }
                for sn in snaps
            ],
            "allocation_history": [
                {
                    "t": ts(block_ts),
                    "source_alloc_pct": s(sg.source_alloc_pct),
                    "source_cost": s(sg.source_cost),
                    "source_equity": s(sg.source_equity),
                    "signal_status": sg.status,
                }
                for sg, block_ts in signals
                if sg.side == "BUY"
            ],
            "positions": [
                {
                    "token_id": p.token_id,
                    "condition_id": p.condition_id,
                    "title": p.title,
                    "outcome": p.outcome,
                    "size": s(p.size),
                    "avg_price": s(p.avg_price),
                    "current_price": s(p.current_price),
                    "current_value": s(p.current_value),
                    "realized_pnl": s(p.realized_pnl),
                    "unrealized_pnl": s(p.unrealized_pnl),
                }
                for p in pos
            ],
        }
    )
    return summary


# --------------------------------------------------------------------- system
def _p(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, round(q * (len(values) - 1)))
    return values[idx]


async def system_metrics(rt: Runtime) -> dict[str, Any]:
    async with rt.sm() as sess:
        evs = (
            await sess.execute(
                select(SourceEvent.block_ts, SourceEvent.detected_at)
                .where(SourceEvent.is_backfill.is_(False))
                .order_by(SourceEvent.id.desc())
                .limit(500)
            )
        ).all()
        sig_lat = (
            await sess.scalars(
                select(CopySignal.signal_latency_ms)
                .where(CopySignal.signal_latency_ms.is_not(None))
                .order_by(CopySignal.id.desc())
                .limit(500)
            )
        ).all()
        ords = (
            await sess.execute(
                select(Order.exec_latency_ms, Order.book_age_ms)
                .where(Order.exec_latency_ms.is_not(None))
                .order_by(Order.id.desc())
                .limit(500)
            )
        ).all()
    lags = [int((d - b).total_seconds() * 1000) for b, d in evs]
    return {
        "source_detection_lag_ms_p50": _p(lags, 0.5),
        "source_detection_lag_ms_p95": _p(lags, 0.95),
        "data_api_lag_s": rt.data_api_lag_s,
        "signal_latency_ms_p50": _p([int(x) for x in sig_lat if x is not None], 0.5),
        "exec_latency_ms_p50": _p([int(e) for e, _ in ords if e is not None], 0.5),
        "book_age_ms_p50": _p([int(b) for _, b in ords if b is not None], 0.5),
        "ws_connected": bool(rt.stream and rt.stream.connected),
        "ws_reconnects": rt.stream.reconnects if rt.stream else 0,
        "chain_stream_connected": rt.feed.stream_connected,
        "chain_stream_reconnects": rt.feed.stream_reconnects,
        "chain_detection_lag_s": rt.feed.last_lag_s,
        "http_errors_5m": rt.http.stats.errors_since(300),
        "last_poll_at": ts(rt.feed.last_tick_at),
    }


def worker_status(rt: Runtime) -> dict[str, Any]:
    return {
        name: {"state": st.state, "last_run_at": ts(st.last_run_at), "last_error": st.last_error}
        for name, st in rt.supervisor.status.items()
    }


async def system_events(rt: Runtime, limit: int) -> dict[str, Any]:
    async with rt.sm() as sess:
        rows = (
            await sess.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(limit))
        ).all()
    return {
        "events": [
            {
                "id": r.id,
                "level": r.level,
                "kind": r.kind,
                "message": r.message,
                "data": r.data,
                "created_at": ts(r.created_at),
            }
            for r in rows
        ]
    }
