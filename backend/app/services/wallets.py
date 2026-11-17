"""Watched-wallet registry and WalletMonitorService (incremental activity ingestion)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import WatchedWallet
from app.integrations.http import UpstreamError, backoff_delay
from app.integrations.polymarket.data_api import DataApiClient
from app.services.copy_signal import CopySignalService
from app.services.equity_estimator import WalletEquityEstimator
from app.services.event_bus import EventBus
from app.services.event_store import TradeEventStore
from app.services.system_events import SystemEventLog

log = logging.getLogger(__name__)

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletError(Exception):
    pass


class DuplicateWallet(WalletError):
    pass


def normalize_address(address: str) -> str:
    address = address.strip()
    if not ADDRESS_RE.match(address):
        raise WalletError("address must be 0x followed by 40 hex characters")
    return address.lower()


class WalletRegistry:
    def __init__(self, sm: async_sessionmaker[AsyncSession], clock: Clock, bus: EventBus) -> None:
        self._sm = sm
        self._clock = clock
        self._bus = bus

    async def add(self, address: str, nickname: str = "", follow: bool = True) -> WatchedWallet:
        addr = normalize_address(address)
        now = self._clock.now()
        async with self._sm() as s, s.begin():
            existing = await s.scalar(select(WatchedWallet).where(WatchedWallet.address == addr))
            if existing is not None and existing.removed_at is None:
                raise DuplicateWallet(addr)
            if existing is not None:
                existing.removed_at = None
                existing.follow = follow
                existing.nickname = nickname or existing.nickname
                existing.followed_since = now
                wallet = existing
            else:
                wallet = WatchedWallet(
                    address=addr, nickname=nickname, follow=follow, added_at=now, followed_since=now
                )
                s.add(wallet)
            await s.flush()
        self._bus.publish("wallet", {"id": wallet.id})
        return wallet

    async def update(self, wallet_id: int, fields: dict[str, Any]) -> WatchedWallet:
        allowed = {"nickname", "follow", "sizing_override"}
        async with self._sm() as s, s.begin():
            wallet = await s.get(WatchedWallet, wallet_id, with_for_update=True)
            if wallet is None or wallet.removed_at is not None:
                raise WalletError("wallet not found")
            for k, v in fields.items():
                if k in allowed:
                    setattr(wallet, k, v)
            if fields.get("follow") is True:
                # Re-enabling copying never back-copies trades made while copying was off.
                wallet.followed_since = max(wallet.followed_since, self._clock.now())
        self._bus.publish("wallet", {"id": wallet_id})
        return wallet

    async def remove(self, wallet_id: int) -> None:
        async with self._sm() as s, s.begin():
            res = await s.execute(
                update(WatchedWallet)
                .where(WatchedWallet.id == wallet_id, WatchedWallet.removed_at.is_(None))
                .values(removed_at=self._clock.now(), follow=False)
            )
            if res.rowcount == 0:  # type: ignore[attr-defined]
                raise WalletError("wallet not found")
        self._bus.publish("wallet", {"id": wallet_id})

    async def active(self) -> list[WatchedWallet]:
        async with self._sm() as s:
            return list(
                (
                    await s.scalars(
                        select(WatchedWallet)
                        .where(WatchedWallet.removed_at.is_(None))
                        .order_by(WatchedWallet.id)
                    )
                ).all()
            )

    async def get(self, wallet_id: int) -> WatchedWallet | None:
        async with self._sm() as s:
            return await s.get(WatchedWallet, wallet_id)


@dataclass(frozen=True, slots=True)
class PollResult:
    fetched: int
    new_events: int
    new_trades: int
    malformed: int


class WalletMonitorService:
    """Polls each wallet's activity feed incrementally from a persisted high-water mark.

    * Re-reads ``overlap`` seconds behind the mark so late-indexed rows are caught; the
      unique event key makes the overlap harmless.
    * On restart the mark resumes polling where it left off (missed data is recovered).
      Recovered trades are still evaluated, and the risk engine rejects stale ones.
    * Events are processed oldest-first per wallet so a BUY is applied before a later SELL.
    """

    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        data_api: DataApiClient,
        store: TradeEventStore,
        copy: CopySignalService,
        estimator: WalletEquityEstimator,
        registry: WalletRegistry,
        events: SystemEventLog,
        bus: EventBus,
        clock: Clock,
        *,
        poll_interval_s: float,
        equity_interval_s: float,
        overlap_s: int,
        backfill: timedelta = timedelta(hours=48),
    ) -> None:
        self._sm = sm
        self._data = data_api
        self._store = store
        self._copy = copy
        self._est = estimator
        self._registry = registry
        self._events = events
        self._bus = bus
        self._clock = clock
        self._poll_interval = poll_interval_s
        self._equity_interval = equity_interval_s
        self._overlap = timedelta(seconds=overlap_s)
        self._backfill = backfill
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self.last_poll_at = None

    async def poll_once(self, wallet: WatchedWallet) -> PollResult:
        now = self._clock.now()
        start = (wallet.hwm_ts - self._overlap) if wallet.hwm_ts else now - self._backfill
        batch = await self._data.activity_since(wallet.address, start)
        detected_at = self._clock.now()
        new = await self._store.ingest(
            wallet.id, batch.rows, detected_at=detected_at, followed_since=wallet.followed_since
        )
        trades = [e for e in new if e.type == "TRADE"]
        hwm = max((r.block_ts for r in batch.rows), default=wallet.hwm_ts)
        last_trade = max((e.block_ts for e in trades), default=None)
        async with self._sm() as s, s.begin():
            row = await s.get(WatchedWallet, wallet.id, with_for_update=True)
            assert row is not None
            if hwm is not None and (row.hwm_ts is None or hwm > row.hwm_ts):
                row.hwm_ts = hwm
            if last_trade is not None and (
                row.last_trade_at is None or last_trade > row.last_trade_at
            ):
                row.last_trade_at = last_trade
            row.last_polled_at = now
            row.last_success_at = detected_at
            row.last_error = None
            row.consecutive_failures = 0
            wallet = row
        self.last_poll_at = detected_at
        if batch.malformed:
            await self._events.record(
                "MALFORMED_ACTIVITY",
                f"{batch.malformed} malformed rows",
                level="WARNING",
                data={"wallet": wallet.address},
            )
        for ev in new:
            if not ev.is_backfill:
                self._bus.publish("source_trade", {"id": ev.id})
        # No equity snapshot here: it adds seconds before copying. The periodic snapshot in
        # run_wallet plus estimate_at's roll-forward give the pre-trade estimate.
        for ev in trades:
            await self._copy.process(ev, wallet)
        if new:
            self._bus.publish("wallet", {"id": wallet.id})
        return PollResult(len(batch.rows), len(new), len(trades), batch.malformed)

    async def _record_failure(self, wallet: WatchedWallet, exc: Exception) -> int:
        async with self._sm() as s, s.begin():
            row = await s.get(WatchedWallet, wallet.id, with_for_update=True)
            assert row is not None
            row.last_polled_at = self._clock.now()
            row.last_error = str(exc)[:500]
            row.consecutive_failures += 1
            failures = row.consecutive_failures
        if failures in (1, 5) or failures % 50 == 0:
            await self._events.record(
                "WALLET_POLL_FAILED",
                f"Polling {wallet.address} failed: {exc}",
                level="WARNING",
                data={"failures": failures},
            )
        self._bus.publish("wallet", {"id": wallet.id})
        return failures

    async def run_wallet(self, wallet_id: int, stop: asyncio.Event) -> None:
        last_equity = None
        while not stop.is_set():
            wallet = await self._registry.get(wallet_id)
            if wallet is None or wallet.removed_at is not None:
                return
            delay = self._poll_interval
            try:
                now = self._clock.now()
                if (
                    last_equity is None
                    or (now - last_equity).total_seconds() >= self._equity_interval
                ):
                    await self._est.snapshot(wallet)
                    last_equity = now
                await self.poll_once(wallet)
            except UpstreamError as exc:
                failures = await self._record_failure(wallet, exc)
                delay = max(delay, backoff_delay(failures, base=self._poll_interval, cap=120))
            except Exception as exc:
                log.exception("wallet monitor error", extra={"wallet_id": wallet_id})
                failures = await self._record_failure(wallet, exc)
                delay = max(delay, backoff_delay(failures, base=self._poll_interval, cap=120))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def supervise(self, stop: asyncio.Event) -> None:
        """Keep one polling task per active wallet; react to adds/removes."""
        try:
            while not stop.is_set():
                active = {w.id for w in await self._registry.active()}
                for wid in active - self._tasks.keys():
                    self._tasks[wid] = asyncio.create_task(
                        self.run_wallet(wid, stop), name=f"wallet-{wid}"
                    )
                for wid, task in list(self._tasks.items()):
                    if wid not in active or task.done():
                        if not task.done():
                            task.cancel()
                        self._tasks.pop(wid, None)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
        finally:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()
