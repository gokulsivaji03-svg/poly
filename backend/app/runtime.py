"""Composition root: builds every service once and owns the worker lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ExecutionMode, Settings
from app.core.clock import Clock, SystemClock
from app.core.money import ZERO
from app.db.models import AccountMode
from app.db.session import make_engine, make_sessionmaker
from app.integrations.http import ResilientHttp
from app.integrations.polygon import CollateralReader, PolygonRpc
from app.integrations.polymarket.clob import ClobRestBooks, MarketStream
from app.integrations.polymarket.data_api import DataApiClient
from app.integrations.polymarket.geoblock import GeoblockChecker
from app.integrations.polymarket.live import (
    US_VENUE,
    LiveGate,
    LiveOrderClient,
    LiveTradingUnavailable,
)
from app.integrations.polymarket.markets import MarketMetadataClient
from app.services.book_recorder import BookRecorder
from app.services.chain_watcher import ChainFillWatcher
from app.services.copy_signal import CopySignalService
from app.services.equity_estimator import WalletEquityEstimator
from app.services.event_bus import EventBus
from app.services.event_store import TradeEventStore
from app.services.hedge_scanner import HedgeScannerService
from app.services.ledger import PortfolioLedger
from app.services.live_execution import LiveExecutionEngine, sdk_client_factory
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.replay import ReplayService
from app.services.settings_service import SettingsService
from app.services.system_events import SystemEventLog
from app.services.wallets import WalletMonitorService, WalletRegistry
from app.services.workers import Supervisor, periodic

log = logging.getLogger(__name__)


async def _live_client(cfg: Settings) -> LiveOrderClient:
    if cfg.live_private_key is None or not cfg.live_funder_address:
        raise LiveTradingUnavailable("live credentials not configured")
    return await sdk_client_factory(
        cfg.live_private_key.get_secret_value(), cfg.live_funder_address
    )


@dataclass
class Runtime:
    cfg: Settings
    clock: Clock
    engine: AsyncEngine
    sm: async_sessionmaker[AsyncSession]
    http: ResilientHttp
    bus: EventBus
    events: SystemEventLog
    data_api: DataApiClient
    market_data: MarketDataService
    stream: MarketStream | None
    ledger: PortfolioLedger
    executor: PaperExecutionEngine
    settings: SettingsService
    estimator: WalletEquityEstimator
    registry: WalletRegistry
    copy: CopySignalService
    monitor: WalletMonitorService
    geoblock: GeoblockChecker
    hedger: HedgeScannerService
    gate: LiveGate
    recorder: BookRecorder
    replay: ReplayService
    supervisor: Supervisor
    sdk: Any = None
    chain: ChainFillWatcher | None = None
    data_api_lag_s: int | None = None
    live_block_reason: str | None = None

    @property
    def account_id(self) -> int:
        return self.copy.account_id

    @classmethod
    async def create(
        cls,
        cfg: Settings,
        *,
        clock: Clock | None = None,
        engine: AsyncEngine | None = None,
        http: ResilientHttp | None = None,
        rest_books: Any = None,
        meta: Any = None,
        stream: MarketStream | None = None,
        enable_stream: bool = True,
    ) -> Runtime:
        clock = clock or SystemClock()
        engine = engine or make_engine(cfg.database_url)
        sm = make_sessionmaker(engine)
        http = http or ResilientHttp(timeout_s=cfg.http_timeout_s, max_retries=cfg.http_max_retries)
        bus = EventBus()
        events = SystemEventLog(sm, bus, clock)
        data_api = DataApiClient(http, cfg.data_api_url)
        sdk = None
        if rest_books is None:
            from polymarket import AsyncPublicClient

            sdk = AsyncPublicClient()
            rest_books = ClobRestBooks(sdk, clock)
        meta = meta or MarketMetadataClient(http, cfg.gamma_api_url, data_api, clock)
        if stream is None and enable_stream:
            stream = MarketStream(cfg.clob_ws_url, clock)
        market_data = MarketDataService(meta, rest_books, clock, stream)
        ledger = PortfolioLedger(sm, clock)
        geoblock = GeoblockChecker(http, cfg.geoblock_url, clock)
        gate = LiveGate(cfg, geoblock)
        settings = SettingsService(sm)
        strategy = await settings.get()
        executor: PaperExecutionEngine
        if cfg.execution_mode is ExecutionMode.LIVE:
            executor = LiveExecutionEngine(
                sm,
                ledger,
                market_data,
                clock,
                gate=gate,
                client_factory=lambda: _live_client(cfg),
            )
            # Live cash is synced from the exchange balance before workers start.
            account_id = await ledger.ensure_account(AccountMode.LIVE, ZERO)
        else:
            executor = PaperExecutionEngine(sm, ledger, market_data, clock)
            account_id = await ledger.ensure_account(
                AccountMode.PAPER, strategy.paper.starting_bankroll_usd
            )
        estimator = WalletEquityEstimator(
            sm, data_api, CollateralReader(http, cfg.polygon_rpc_url), clock
        )
        registry = WalletRegistry(sm, clock, bus)
        copy = CopySignalService(
            sm, ledger, executor, market_data, estimator, settings, bus, clock, account_id
        )
        store = TradeEventStore(sm)
        monitor = WalletMonitorService(
            sm,
            data_api,
            store,
            copy,
            estimator,
            registry,
            events,
            bus,
            clock,
            poll_interval_s=cfg.wallet_poll_interval_s,
            equity_interval_s=cfg.equity_snapshot_interval_s,
            overlap_s=cfg.ingest_overlap_s,
            backfill=timedelta(hours=cfg.backfill_hours),
        )
        chain = None
        if cfg.chain_watch_enabled and cfg.polygon_rpc_url and isinstance(
            meta, MarketMetadataClient
        ):
            chain = ChainFillWatcher(
                PolygonRpc(http, cfg.polygon_rpc_url),
                meta,
                registry,
                store,
                copy,
                bus,
                clock,
                poll_interval_s=cfg.chain_poll_interval_s,
                lookback_blocks=cfg.chain_lookback_blocks,
            )
        rt = cls(
            cfg=cfg,
            clock=clock,
            engine=engine,
            sm=sm,
            http=http,
            bus=bus,
            events=events,
            data_api=data_api,
            market_data=market_data,
            stream=stream,
            ledger=ledger,
            executor=executor,
            settings=settings,
            estimator=estimator,
            registry=registry,
            copy=copy,
            monitor=monitor,
            geoblock=geoblock,
            gate=gate,
            hedger=HedgeScannerService(
                sm, ledger, executor, market_data, settings, events, bus, clock, account_id
            ),
            recorder=BookRecorder(sm, market_data, clock),
            replay=ReplayService(engine, sm, market_data, clock),
            supervisor=Supervisor(engine, clock),
            sdk=sdk,
            chain=chain,
        )
        rt._register_workers()
        return rt

    def _register_workers(self) -> None:
        sup = self.supervisor
        sup.on_leader = self.recover
        sup.add("wallet_monitor", self.monitor.supervise)
        if self.stream is not None:
            sup.add("market_stream", self.stream.run)
        if self.chain is not None:
            self.chain.on_tick = lambda: sup.heartbeat("chain_watcher")
            sup.add("chain_watcher", self.chain.run)
        sup.add(
            "portfolio_snapshots",
            lambda stop: periodic(
                stop,
                self.cfg.portfolio_snapshot_interval_s,
                self.snapshot_portfolio,
                on_tick=lambda: sup.heartbeat("portfolio_snapshots"),
            ),
        )
        sup.add(
            "upstream_health",
            lambda stop: periodic(
                stop, 60, self.check_upstream, on_tick=lambda: sup.heartbeat("upstream_health")
            ),
        )
        sup.add(
            "reconciliation",
            lambda stop: periodic(
                stop, 300, self.reconcile, on_tick=lambda: sup.heartbeat("reconciliation")
            ),
        )
        sup.add("hedge_scanner", self.hedger.run)
        sup.add(
            "book_recorder",
            lambda stop: periodic(
                stop, 15, self.recorder.tick, on_tick=lambda: sup.heartbeat("book_recorder")
            ),
        )
        sup.add(
            "settlement",
            lambda stop: periodic(
                stop, 60, self.settle, on_tick=lambda: sup.heartbeat("settlement")
            ),
        )

    async def settle(self) -> None:
        await self.hedger.settle_resolved()

    async def recover(self) -> None:
        """Startup reconciliation, run once by the leader before workers start."""
        failed = await self.executor.recover_pending()
        if failed:
            await self.events.record(
                "RECOVERED_PENDING_ORDERS",
                f"Failed {failed} orders left PENDING by a crash",
                level="WARNING",
            )
        hedges = await self.hedger.recover()
        if hedges:
            await self.events.record(
                "RECOVERED_PENDING_HEDGES",
                f"Failed {hedges} hedges left pending by a crash",
                level="WARNING",
            )
        resumed = await self.copy.resume_unfinished()
        if resumed:
            await self.events.record("RESUMED_SIGNALS", f"Resumed {resumed} unfinished signals")
        await self.reconcile()

    async def reconcile(self) -> None:
        problems = await self.ledger.verify(self.account_id)
        if problems:
            await self.events.record(
                "LEDGER_MISMATCH", "; ".join(problems), level="ERROR", data={"problems": problems}
            )

    async def snapshot_portfolio(self) -> None:
        view = await self.copy.portfolio()
        await self.ledger.record_snapshot(view)
        self.bus.publish("portfolio", {})

    async def check_upstream(self) -> None:
        try:
            self.data_api_lag_s = await self.data_api.status_lag_seconds()
        except Exception as exc:
            log.info("data api status unavailable", extra={"error": str(exc)})
            self.data_api_lag_s = None
        await self.geoblock.check()

    async def start(self) -> None:
        if not self.cfg.run_workers:
            return
        if self.cfg.execution_mode is ExecutionMode.LIVE:
            status = await self.gate.check(fresh=True)
            if not status.live_enabled:
                # Restricted or misconfigured: never start trading workers in LIVE mode.
                self.live_block_reason = status.reason
                await self.events.record(
                    "LIVE_TRADING_BLOCKED", status.reason or "blocked", level="ERROR"
                )
                return
            await self._sync_live_balance()
        await self.supervisor.start()

    async def _sync_live_balance(self) -> None:
        assert isinstance(self.executor, LiveExecutionEngine)
        client = await _live_client(self.cfg)
        info = await client.get_balance_allowance(asset_type="COLLATERAL")  # type: ignore[attr-defined]
        balance = Decimal(int(info.balance)) / Decimal(10**6)
        await self.ledger.sync_cash(self.account_id, balance, "Exchange collateral balance sync")

    async def venue_status(self) -> list[dict[str, Any]]:
        intl = await self.gate.check(fresh=False)
        return [intl.to_dict(), US_VENUE.to_dict()]

    async def stop(self) -> None:
        await self.replay.wait()
        await self.supervisor.stop()
        await self.http.aclose()
        if self.sdk is not None:
            await self.sdk.close()
        await self.engine.dispose()
