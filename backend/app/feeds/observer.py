"""WalletObserver: periodic public observation of watched wallets.

Each pass refreshes every active wallet's equity snapshot (Data API value/positions/P&L
plus on-chain collateral), which sizing reads, and records the observation's health on the
wallet. The first successful pass also imports the wallet's recent history from the Data
API for analytics. Only activity from before ``followed_since`` is imported: live trades
come exclusively from the chain feed, so an imported row can never pre-empt a copy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import SourceEvent, WatchedWallet
from app.feeds.tape import TradeTape
from app.integrations.polymarket.data_api import DataApiClient
from app.services.equity_estimator import WalletEquityEstimator
from app.services.event_bus import EventBus
from app.services.wallets import WalletRegistry

log = logging.getLogger(__name__)


class WalletObserver:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        estimator: WalletEquityEstimator,
        data_api: DataApiClient,
        tape: TradeTape,
        registry: WalletRegistry,
        bus: EventBus,
        clock: Clock,
        *,
        interval_s: float,
        history: timedelta,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self._sm = sm
        self._est = estimator
        self._data = data_api
        self._tape = tape
        self._registry = registry
        self._bus = bus
        self._clock = clock
        self._interval = interval_s
        self._history = history
        self.on_tick = on_tick
        self._imported: set[int] = set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.observe_all()
            if self.on_tick:
                self.on_tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)

    async def observe_all(self) -> None:
        for wallet in await self._registry.active():
            await self.observe(wallet)

    async def observe(self, wallet: WatchedWallet) -> None:
        now = self._clock.now()
        error: str | None
        try:
            snapshot = await self._est.snapshot(wallet)
            error = snapshot.error
            if error is None and wallet.id not in self._imported:
                await self.import_history(wallet)
        except Exception as exc:  # observation is best-effort; record it and move on
            log.exception("wallet observation failed", extra={"wallet_id": wallet.id})
            error = f"{type(exc).__name__}: {exc}"
        async with self._sm() as s, s.begin():
            row = await s.get(WatchedWallet, wallet.id, with_for_update=True)
            assert row is not None
            row.last_polled_at = now
            row.last_error = error[:500] if error else None
            if error is None:
                row.last_success_at = now
                row.consecutive_failures = 0
            else:
                row.consecutive_failures += 1
        self._bus.publish("wallet", {"id": wallet.id})

    async def import_history(self, wallet: WatchedWallet) -> int:
        """Import pre-follow activity once; returns rows inserted (0 if already present)."""
        async with self._sm() as s:
            seen = await s.scalar(
                select(SourceEvent.id).where(SourceEvent.wallet_id == wallet.id).limit(1)
            )
        inserted = 0
        if seen is None:
            since = wallet.followed_since - self._history
            batch = await self._data.activity_since(wallet.address, since)
            rows = [r for r in batch.rows if r.block_ts < wallet.followed_since]
            new = await self._tape.ingest(
                wallet.id,
                rows,
                detected_at=self._clock.now(),
                followed_since=wallet.followed_since,
                source="api",
            )
            inserted = len(new)
        self._imported.add(wallet.id)
        return inserted
