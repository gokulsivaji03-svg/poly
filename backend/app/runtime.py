"""Composition root: builds every service once and owns the worker lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ExecutionMode, Settings
from app.core.clock import Clock, SystemClock
from app.db.models import AccountMode
from app.db.session import make_engine, make_sessionmaker
from app.integrations.http import ResilientHttp
from app.integrations.polygon import CollateralReader
from app.integrations.polymarket.clob import ClobRestBooks, MarketStream
from app.integrations.polymarket.data_api import DataApiClient
from app.integrations.polymarket.geoblock import GeoblockChecker
from app.integrations.polymarket.markets import MarketMetadataClient
from app.services.copy_signal import CopySignalService
from app.services.equity_estimator import WalletEquityEstimator
from app.services.event_bus import EventBus
from app.services.event_store import TradeEventStore
from app.services.ledger import PortfolioLedger
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.settings_service import SettingsService
from app.services.system_events import SystemEventLog
from app.services.wallets import WalletMonitorService, WalletRegistry
from app.services.workers import Supervisor, periodic

log = logging.getLogger(__name__)


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
    supervisor: Supervisor
    sdk: Any = None
    data_api_lag_s: int | None = None

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
        mode = (
            ExecutionMode.REPLAY
            if cfg.execution_mode is ExecutionMode.REPLAY
            else (ExecutionMode.PAPER)
        )
        executor = PaperExecutionEngine(sm, ledger, market_data, clock, mode=mode)
        settings = SettingsService(sm)
        strategy = await settings.get()
        account_id = await ledger.ensure_account(
            AccountMode(mode.value), strategy.paper.starting_bankroll_usd
        )
        estimator = WalletEquityEstimator(
            sm, data_api, CollateralReader(http, cfg.polygon_rpc_url), clock
        )
        registry = WalletRegistry(sm, clock, bus)
        copy = CopySignalService(
            sm, ledger, executor, market_data, estimator, settings, bus, clock, account_id
        )
        monitor = WalletMonitorService(
            sm,
            data_api,
            TradeEventStore(sm),
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
        geoblock = GeoblockChecker(http, cfg.geoblock_url, clock)
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
            supervisor=Supervisor(engine, clock),
            sdk=sdk,
        )
        rt._register_workers()
        return rt

    def _register_workers(self) -> None:
        sup = self.supervisor
        sup.on_leader = self.recover
        sup.add("wallet_monitor", self.monitor.supervise)
        if self.stream is not None:
            sup.add("market_stream", self.stream.run)
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

    async def recover(self) -> None:
        """Startup reconciliation, run once by the leader before workers start."""
        failed = await self.executor.recover_pending()
        if failed:
            await self.events.record(
                "RECOVERED_PENDING_ORDERS",
                f"Failed {failed} orders left PENDING by a crash",
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
        if self.cfg.run_workers:
            await self.supervisor.start()

    async def stop(self) -> None:
        await self.supervisor.stop()
        await self.http.aclose()
        if self.sdk is not None:
            await self.sdk.close()
        await self.engine.dispose()
