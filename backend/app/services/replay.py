"""Historical replay: the real copy/hedge pipeline over past trades, isolated in a schema.

Each run gets its own PostgreSQL schema (``replay_<id>``) created from the same metadata,
populated with the recorded wallets, source events, equity snapshots and book snapshots up
to the run's end, and driven by a ManualClock.

With ``prices="history"`` (the default) the run also loads what was never recorded here:
the wallets' activity from the Data API, and Gamma metadata and CLOB minute bars for every
traded token (see ``replay_history``). Recorded books win whenever they are newer than the
last bar. Markets settle at their on-chain payouts once they close.

Look-ahead is prevented structurally:

* books: only the latest snapshot or minute bar at or before the replay clock;
* source equity: only snapshots observed at or before the replay clock, except that a wallet
  with none before the window starts from its earliest snapshot (reported in the summary);
* source positions (for sell sizing): reconstructed from events at or before the clock;
* market metadata: recorded metadata at or before the clock, else open until ``closed_at``.

Results are written back to ``replay_runs`` and the schema is dropped. The live paper account
is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import make_transient

from app.config import ExecutionMode
from app.core.clock import ManualClock
from app.core.money import ONE, ZERO, to_decimal
from app.db.base import Base
from app.db.models import (
    AccountMode,
    AppSettings,
    BookSnapshot,
    CopySignal,
    ReplayRun,
    SourceEvent,
    WalletEquitySnapshot,
    WatchedWallet,
)
from app.domain.equity import EquityCoverage
from app.domain.market import BookLevel, MarketInfo, OrderBook
from app.domain.settings import StrategySettings
from app.engine.context import ContextLoader
from app.engine.copy_engine import CopyEngine
from app.engine.journal import SignalJournal
from app.engine.portfolio import PortfolioReader
from app.execution.paper import PaperExecutor
from app.feeds.tape import TradeTape
from app.services.equity_estimator import WalletEquityEstimator
from app.services.event_bus import EventBus
from app.services.hedge_scanner import HedgeScannerService
from app.services.ledger import PortfolioLedger
from app.services.market_data import MarketDataService
from app.services.replay_history import DEPTH, HistoryClient, MarketHistory, equity_path
from app.services.settings_service import SettingsService, deep_merge
from app.services.system_events import SystemEventLog

log = logging.getLogger(__name__)


class ReplayParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    wallet_ids: list[int] = Field(min_length=1, max_length=50)
    starting_bankroll: Decimal = Field(default=Decimal("1000"), gt=0)
    detection_delay_s: int = Field(default=2, ge=0, le=3600)
    prices: Literal["history", "source", "recorded"] = "history"
    """``history`` fills at minute bars; ``source`` fills at the source's own price plus a
    random premium; ``recorded`` uses only what this server recorded."""
    premium_max: Decimal = Field(default=Decimal("0.05"), ge=0, le=Decimal("0.5"))
    """``source`` mode: each copy pays the source price plus a uniform 0..premium_max."""
    seed: int = 0
    """``source`` mode: seeds the premiums, so a run repeats exactly."""
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _window(self) -> ReplayParams:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must include a timezone")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > timedelta(days=31):
            raise ValueError("replay window is limited to 31 days")
        return self


class ReplayBusy(Exception):
    pass


class HistoricalBooks:
    """RestBookSource serving the latest recorded snapshot at or before the replay clock."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], clock: ManualClock) -> None:
        self._sm = sm
        self._clock = clock
        self.served = 0

    async def fetch(self, token_ids: list[str]) -> dict[str, OrderBook]:
        now = self._clock.now()
        out: dict[str, OrderBook] = {}
        async with self._sm() as s:
            for token in token_ids:
                snap = await s.scalar(
                    select(BookSnapshot)
                    .where(BookSnapshot.token_id == token, BookSnapshot.received_at <= now)
                    .order_by(BookSnapshot.received_at.desc())
                    .limit(1)
                )
                if snap is None:
                    continue
                out[token] = OrderBook(
                    token_id=token,
                    condition_id=snap.condition_id,
                    bids=_levels(snap.bids),
                    asks=_levels(snap.asks),
                    tick_size=snap.tick_size,
                    min_order_size=snap.min_order_size,
                    received_at=snap.received_at,
                    hash=snap.hash,
                )
                self.served += 1
        return out


def _levels(doc: dict[str, Any]) -> tuple[BookLevel, ...]:
    return tuple(
        BookLevel(price=to_decimal(p), size=to_decimal(sz)) for p, sz in doc.get("levels", [])
    )


class PointInTimeMetadata:
    """Metadata recorded alongside books; never consult a present-day upstream."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], clock: ManualClock) -> None:
        self._sm, self._clock = sm, clock

    async def fetch(self, condition_id: str) -> MarketInfo | None:
        async with self._sm() as s:
            snapshots = await s.scalars(
                select(BookSnapshot)
                .where(
                    BookSnapshot.condition_id == condition_id,
                    BookSnapshot.received_at <= self._clock.now(),
                )
                .order_by(BookSnapshot.received_at.desc())
                .limit(1)
            )
            snap = snapshots.first()
        if snap is None or not snap.asks.get("market"):
            return None
        return MarketInfo.model_validate(snap.asks["market"])


def premium(seed: int, event_key: str, cap: Decimal) -> Decimal:
    """A uniform draw in [0, cap] fixed by the seed and the event, to 0.1 cent."""
    digest = hashlib.sha256(f"{seed}:{event_key}".encode()).digest()
    fraction = Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**64)
    return (cap * fraction).quantize(Decimal("0.001"))


SOURCE_LOOKBACK = timedelta(days=7)
"""Activity fetched before the window, so sells size against positions opened earlier."""
EQUITY_GRID = timedelta(minutes=30)
"""Spacing of back-filled equity snapshots: confidence halves every 2 h from a snapshot."""


class HistoryBooks:
    """Recorded books, or the last minute bar where it is newer."""

    def __init__(
        self,
        recorded: HistoricalBooks,
        history: MarketHistory,
        conditions: dict[str, str],
        clock: ManualClock,
    ) -> None:
        self._recorded, self._history = recorded, history
        self._conditions, self._clock = conditions, clock
        self.bars_served = 0
        self._quotes: dict[str, OrderBook] = {}

    def quote(self, token_id: str, condition_id: str, price: Decimal, premium: Decimal) -> None:
        """Until ``unquote``: buys fill at ``price + premium``, sells at ``price - premium``."""
        info = self._history.markets.get(condition_id)
        tick = info.tick_size if info else Decimal("0.01")
        ask = min(price + premium, ONE - Decimal("0.001"))
        bid = price - premium
        self._quotes[token_id] = OrderBook(
            token_id=token_id,
            condition_id=condition_id,
            asks=(BookLevel(price=ask, size=DEPTH),),
            bids=(BookLevel(price=bid, size=DEPTH),) if bid > 0 else (),
            tick_size=min(tick, Decimal("0.001")),
            min_order_size=info.min_order_size if info else ZERO,
            received_at=self._clock.now(),
            hash=f"quote:{int(self._clock.now().timestamp())}",
        )

    def unquote(self, token_id: str) -> None:
        self._quotes.pop(token_id, None)

    async def fetch(self, token_ids: list[str]) -> dict[str, OrderBook]:
        out = await self._recorded.fetch(token_ids)
        now = self._clock.now()
        for token in token_ids:
            if token in self._quotes:
                out[token] = self._quotes[token]
                continue
            cid = self._conditions.get(token)
            bar = self._history.book_at(token, cid, now) if cid else None
            if bar is not None and (token not in out or bar.received_at > out[token].received_at):
                out[token] = bar
                self.bars_served += 1
        return out


class HistoryMetadata:
    """Recorded metadata, else Gamma's as it stood at the replay clock."""

    def __init__(
        self, recorded: PointInTimeMetadata, history: MarketHistory, clock: ManualClock
    ) -> None:
        self._recorded, self._history, self._clock = recorded, history, clock

    async def fetch(self, condition_id: str) -> MarketInfo | None:
        return await self._recorded.fetch(condition_id) or self._history.market_at(
            condition_id, self._clock.now()
        )


class ReplayEstimator(WalletEquityEstimator):
    point_in_time = True

    async def snapshot(self, wallet: WatchedWallet) -> WalletEquitySnapshot:  # pragma: no cover
        raise RuntimeError("replay never observes live wallets")

    async def source_position_size(self, wallet: WatchedWallet, token_id: str) -> Decimal | None:
        now = self._clock.now()
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(SourceEvent.side, func.sum(SourceEvent.size))
                    .where(
                        SourceEvent.wallet_id == wallet.id,
                        SourceEvent.token_id == token_id,
                        SourceEvent.type == "TRADE",
                        SourceEvent.block_ts <= now,
                    )
                    .group_by(SourceEvent.side)
                )
            ).all()
        net = sum(((q if side == "BUY" else -q) for side, q in rows), ZERO)
        return max(ZERO, net)


class ReplayService:
    def __init__(
        self,
        engine: AsyncEngine,
        sm: async_sessionmaker[AsyncSession],
        live_market_data: MarketDataService,
        wall_clock: Any,
        history: HistoryClient | None = None,
    ) -> None:
        self._engine = engine
        self._sm = sm
        self._live_md = live_market_data
        self._wall = wall_clock
        self._history = history
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def submit(self, params: ReplayParams) -> int:
        if self.busy:
            raise ReplayBusy("a replay is already running")
        async with self._sm() as s, s.begin():
            run = ReplayRun(
                status="RUNNING", params=params.model_dump(mode="json"), created_at=self._wall.now()
            )
            s.add(run)
            await s.flush()
            run_id = run.id
        self._task = asyncio.create_task(self._guarded(run_id, params), name=f"replay-{run_id}")
        return run_id

    async def wait(self) -> None:
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task

    async def _guarded(self, run_id: int, params: ReplayParams) -> None:
        try:
            result = await self.run(run_id, params)
            status, error = "COMPLETED", None
        except Exception as exc:
            log.exception("replay failed", extra={"run_id": run_id})
            result, status, error = None, "FAILED", f"{type(exc).__name__}: {exc}"
        async with self._sm() as s, s.begin():
            run = await s.get(ReplayRun, run_id)
            assert run is not None
            run.status, run.result, run.error = status, result, error
            run.finished_at = self._wall.now()

    async def run(self, run_id: int, params: ReplayParams) -> dict[str, Any]:
        schema = f"replay_{run_id}"
        async with self._engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        rengine = self._engine.execution_options(schema_translate_map={None: schema})
        try:
            async with rengine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            rsm = async_sessionmaker(rengine, expire_on_commit=False)
            counts = await self._seed(rsm, schema, params)
            history = None
            if params.prices != "recorded":
                history, counts["fetched"], counts["equity_backfilled"] = await self._fetch(
                    rsm, params
                )
            counts["events"] = await self._count_events(rsm, params)
            return await self._drive(rsm, params, counts, history)
        finally:
            async with self._engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def _seed(
        self, rsm: async_sessionmaker[AsyncSession], schema: str, p: ReplayParams
    ) -> dict[str, int]:
        async with self._sm() as s:
            wallets = list(
                (
                    await s.scalars(select(WatchedWallet).where(WatchedWallet.id.in_(p.wallet_ids)))
                ).all()
            )
            if not wallets:
                raise ValueError("no matching wallets")
            events = list(
                (
                    await s.scalars(
                        select(SourceEvent)
                        .where(
                            SourceEvent.wallet_id.in_(p.wallet_ids), SourceEvent.block_ts <= p.end
                        )
                        .order_by(SourceEvent.id)
                    )
                ).all()
            )
            snaps = list(
                (
                    await s.scalars(
                        select(WalletEquitySnapshot).where(
                            WalletEquitySnapshot.wallet_id.in_(p.wallet_ids),
                            WalletEquitySnapshot.observed_at <= p.end,
                        )
                    )
                ).all()
            )
            tokens = {e.token_id for e in events if e.block_ts >= p.start}
            books: list[BookSnapshot] = []
            if tokens:
                books = list(
                    (
                        await s.scalars(
                            select(BookSnapshot).where(
                                BookSnapshot.condition_id.in_(
                                    {e.condition_id for e in events if e.token_id in tokens}
                                ),
                                BookSnapshot.received_at <= p.end,
                                BookSnapshot.received_at >= p.start - timedelta(hours=1),
                            )
                        )
                    ).all()
                )
            live_settings = await s.get(AppSettings, 1)
        base = (
            live_settings.document
            if live_settings
            else StrategySettings().model_dump(mode="json", by_alias=True)
        )
        if p.prices == "source":
            # The premium is the assumption under test; a drift limit would reject it.
            base = deep_merge(base, {"risk": {"max_price_diff_pct": "1"}})
        doc = deep_merge(
            base,
            {
                **p.settings,
                "strategies_paused": False,
                "emergency_stop": False,
                "paper": {
                    **p.settings.get("paper", {}),
                    "starting_bankroll_usd": str(p.starting_bankroll),
                    "execution_delay_ms": 0,
                },
            },
        )
        StrategySettings.model_validate(doc)  # fail fast on invalid overrides
        async with rsm() as s, s.begin():
            for obj in (*wallets, *events, *snaps, *books):
                make_transient(obj)
            for w in wallets:
                w.follow, w.removed_at, w.followed_since = True, None, p.start
            for e in events:
                e.is_backfill = e.block_ts < p.start
            s.add_all(wallets)
            await s.flush()  # No ORM relationships: explicitly insert parents before events.
            s.add_all([*events, *snaps, *books])
            s.add(AppSettings(id=1, version=1, document=doc))
            await s.flush()
            # Copied rows keep their live ids; rows created during the run must not reuse them.
            for table in (WatchedWallet, SourceEvent, WalletEquitySnapshot, BookSnapshot):
                top = await s.scalar(select(func.max(table.id)))
                await s.execute(
                    text("SELECT setval(pg_get_serial_sequence(:t, 'id'), :v)"),
                    {"t": f"{schema}.{table.__tablename__}", "v": max(top or 1, 1)},
                )
        return {"books": len(books)}

    async def _fetch(
        self, rsm: async_sessionmaker[AsyncSession], p: ReplayParams
    ) -> tuple[MarketHistory, int, int]:
        """Activity, metadata and minute bars into the replay schema; returns the history,
        the activity rows taped and how many wallets had their equity back-filled."""
        if self._history is None:
            raise ValueError("price history is not configured on this server")
        async with rsm() as s:
            wallets = list((await s.scalars(select(WatchedWallet))).all())
        tape = TradeTape(rsm)
        fetched = 0
        for w in wallets:
            rows = await self._history.activity(w.address, p.start - SOURCE_LOOKBACK, p.end)
            new = await tape.ingest(
                w.id, rows, detected_at=p.start, followed_since=p.start, source="api"
            )
            fetched += len(new)
        backfilled = await self._backfill_equity(rsm, p, wallets)
        async with rsm() as s:
            pairs = (
                await s.execute(
                    select(SourceEvent.token_id, SourceEvent.condition_id)
                    .where(
                        SourceEvent.type == "TRADE",
                        SourceEvent.block_ts >= p.start,
                        SourceEvent.block_ts <= p.end,
                        SourceEvent.token_id.is_not(None),
                    )
                    .distinct()
                )
            ).all()
        tokens = {t: c for t, c in pairs if t and c}
        history = await self._history.load(tokens, p.start - timedelta(hours=1), p.end)
        return history, fetched, backfilled

    async def _backfill_equity(
        self, rsm: async_sessionmaker[AsyncSession], p: ReplayParams, wallets: list[WatchedWallet]
    ) -> int:
        """Equity for a wallet first observed after ``start``, so its trades are not all
        skipped for an unknown bankroll.

        It is anchored on the earliest snapshot that saw the wallet's cash and walked back
        hourly along its P&L history (``equity_path``), replacing positions-only snapshots
        before the anchor. Without a cash snapshot or P&L history, the earliest snapshot is
        held flat. Either way it looks ahead on sizing only, never on price."""
        assert self._history is not None
        backfilled = 0
        for w in wallets:
            async with rsm() as s:
                known = await s.scalar(
                    select(WalletEquitySnapshot.id)
                    .where(
                        WalletEquitySnapshot.wallet_id == w.id,
                        WalletEquitySnapshot.observed_at <= p.start,
                        WalletEquitySnapshot.coverage != EquityCoverage.UNAVAILABLE.value,
                    )
                    .limit(1)
                )
            if known is not None:
                continue
            async with self._sm() as s:
                ordered = select(WalletEquitySnapshot).where(WalletEquitySnapshot.wallet_id == w.id)
                ordered = ordered.order_by(WalletEquitySnapshot.observed_at).limit(1)
                anchor = await s.scalar(
                    ordered.where(
                        WalletEquitySnapshot.coverage == EquityCoverage.POSITIONS_AND_CASH.value
                    )
                )
                earliest = await s.scalar(
                    ordered.where(WalletEquitySnapshot.coverage != EquityCoverage.UNAVAILABLE.value)
                )
            rows = await self._walked_back(w, anchor, p) if anchor is not None else []
            if anchor is not None and rows:
                async with rsm() as s, s.begin():
                    await s.execute(
                        delete(WalletEquitySnapshot).where(
                            WalletEquitySnapshot.wallet_id == w.id,
                            WalletEquitySnapshot.observed_at < anchor.observed_at,
                            WalletEquitySnapshot.coverage == EquityCoverage.POSITIONS_ONLY.value,
                        )
                    )
                    s.add_all(rows)
            elif earliest is not None:
                until = min(p.end, earliest.observed_at)
                steps = int((until - p.start) / EQUITY_GRID) + 1
                async with rsm() as s, s.begin():
                    s.add_all(
                        WalletEquitySnapshot(
                            wallet_id=w.id,
                            observed_at=p.start + EQUITY_GRID * i,
                            positions_value=earliest.positions_value,
                            cash_balance=earliest.cash_balance,
                            coverage=earliest.coverage,
                            open_positions=earliest.open_positions,
                        )
                        for i in range(steps)
                    )
            else:
                continue
            backfilled += 1
        return backfilled

    async def _walked_back(
        self, w: WatchedWallet, anchor: WalletEquitySnapshot, p: ReplayParams
    ) -> list[WalletEquitySnapshot]:
        assert self._history is not None
        positions = anchor.positions_value or ZERO
        cash = anchor.cash_balance or ZERO
        equity = positions + cash
        if equity <= 0:
            return []
        path = equity_path(
            equity,
            anchor.observed_at,
            await self._history.pnl(w.address),
            p.start - EQUITY_GRID,
            min(p.end, anchor.observed_at),
        )
        cash_share = cash / equity
        return [
            WalletEquitySnapshot(
                wallet_id=w.id,
                observed_at=at,
                positions_value=e * (1 - cash_share),
                cash_balance=e * cash_share,
                coverage=EquityCoverage.POSITIONS_AND_CASH.value,
            )
            for at, e in path
        ]

    async def _count_events(self, rsm: async_sessionmaker[AsyncSession], p: ReplayParams) -> int:
        async with rsm() as s:
            return (
                await s.scalar(
                    select(func.count())
                    .select_from(SourceEvent)
                    .where(SourceEvent.block_ts >= p.start, SourceEvent.block_ts <= p.end)
                )
            ) or 0

    async def _drive(
        self,
        rsm: async_sessionmaker[AsyncSession],
        p: ReplayParams,
        counts: dict[str, int],
        history: MarketHistory | None,
    ) -> dict[str, Any]:
        clock = ManualClock(p.start)
        bus = EventBus()
        hist = HistoricalBooks(rsm, clock)
        books: HistoricalBooks | HistoryBooks = hist
        meta: PointInTimeMetadata | HistoryMetadata = PointInTimeMetadata(rsm, clock)
        if history is not None:
            async with rsm() as s:
                pairs = (
                    await s.execute(
                        select(SourceEvent.token_id, SourceEvent.condition_id).distinct()
                    )
                ).all()
            books = HistoryBooks(hist, history, {t: c for t, c in pairs if t and c}, clock)
            meta = HistoryMetadata(meta, history, clock)
        md = MarketDataService(meta, books, clock, None)
        ledger = PortfolioLedger(rsm, clock)
        executor = PaperExecutor(rsm, ledger, md, clock, mode=ExecutionMode.REPLAY)
        settings = SettingsService(rsm)
        strategy = await settings.get()
        account_id = await ledger.ensure_account(AccountMode.REPLAY, p.starting_bankroll)
        estimator = ReplayEstimator(rsm, None, None, clock)  # type: ignore[arg-type]
        portfolio = PortfolioReader(ledger, md, clock, account_id)
        copy_engine = CopyEngine(
            rsm,
            SignalJournal(rsm, bus, clock),
            ContextLoader(rsm, md, estimator, portfolio),
            ledger,
            executor,
            settings,
            bus,
            clock,
            account_id,
        )
        hedger = HedgeScannerService(
            rsm,
            ledger,
            executor,
            md,
            settings,
            SystemEventLog(rsm, bus, clock),
            bus,
            clock,
            account_id,
        )
        async with rsm() as s:
            rows = (
                await s.execute(
                    select(SourceEvent, WatchedWallet)
                    .join(WatchedWallet, WatchedWallet.id == SourceEvent.wallet_id)
                    .where(
                        SourceEvent.block_ts >= p.start,
                        SourceEvent.block_ts <= p.end,
                        SourceEvent.type == "TRADE",
                    )
                    .order_by(SourceEvent.block_ts, SourceEvent.id)
                )
            ).all()
        quotes = books if isinstance(books, HistoryBooks) and p.prices == "source" else None
        curve: list[dict[str, Any]] = [
            {"t": p.start.isoformat(), "equity": str(p.starting_bankroll)}
        ]
        for ev, wallet in rows:
            moment = ev.block_ts + timedelta(seconds=p.detection_delay_s)
            if moment > p.end:
                break
            if moment > clock.now():
                clock.set(moment)
            ev.detected_at = clock.now()
            async with rsm() as s, s.begin():
                row = await s.get(SourceEvent, ev.id)
                assert row is not None
                row.detected_at = ev.detected_at
            if history is not None:
                await hedger.settle_resolved()
            if quotes is not None:
                paid = premium(p.seed, ev.event_key, p.premium_max)
                quotes.quote(ev.token_id, ev.condition_id, ev.price, paid)
                await md.fetch_books([ev.token_id])
            await copy_engine.on_trade(ev, wallet)
            if quotes is not None:
                quotes.unquote(ev.token_id)
                await md.fetch_books([ev.token_id])
            if strategy.hedge.enabled:
                await hedger.scan()
            view = await portfolio.view()
            await ledger.record_snapshot(view)
            curve.append({"t": clock.now().isoformat(), "equity": str(view.equity)})
        if p.end > clock.now():
            clock.set(p.end)
        if history is not None:
            await hedger.settle_resolved()
        final = await portfolio.view()
        curve.append({"t": p.end.isoformat(), "equity": str(final.equity)})
        problems = await ledger.verify(account_id)
        async with rsm() as s:
            sigs = (
                await s.execute(
                    select(CopySignal, SourceEvent, WatchedWallet)
                    .join(SourceEvent, SourceEvent.id == CopySignal.source_event_id)
                    .join(WatchedWallet, WatchedWallet.id == CopySignal.wallet_id)
                    .order_by(SourceEvent.block_ts, CopySignal.id)
                )
            ).all()
        by_status = Counter(sig.status for sig, _, _ in sigs)
        ret = (final.equity - p.starting_bankroll) / p.starting_bankroll
        return {
            "summary": {
                "final_equity": str(final.equity),
                "starting_bankroll": str(p.starting_bankroll),
                "return_pct": str(ret.quantize(Decimal("0.0001"))),
                "realized_pnl": str(final.realized_pnl),
                "unrealized_pnl": str(final.unrealized_pnl),
                "locked_profit": str(final.locked_profit),
                "signals": dict(by_status),
                "events_replayed": counts["events"],
                "prices": p.prices,
                **(
                    {"premium_max": str(p.premium_max), "seed": p.seed}
                    if p.prices == "source"
                    else {}
                ),
                "snapshots_used": hist.served,
                "minute_bars_used": books.bars_served if isinstance(books, HistoryBooks) else 0,
                "activity_fetched": counts.get("fetched", 0),
                "equity_backfilled": counts.get("equity_backfilled", 0),
                "ledger_ok": not problems,
            },
            "equity_curve": curve,
            "signals": [
                {
                    "t": ev.block_ts.isoformat(),
                    "wallet": w.nickname or w.address,
                    "side": sig.side,
                    "title": ev.title,
                    "outcome": ev.outcome,
                    "status": sig.status,
                    "source_price": str(sig.source_price),
                    "avg_fill_price": None
                    if sig.avg_fill_price is None
                    else str(sig.avg_fill_price),
                    "filled_cost": None if sig.filled_cost is None else str(sig.filled_cost),
                    "reasons": [r["code"] for r in sig.reasons.get("items", [])],
                    "detail": next(
                        (r.get("message") for r in reversed(sig.reasons.get("items", []))), None
                    ),
                }
                for sig, ev, w in sigs
            ],
        }
