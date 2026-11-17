"""ChainFeed: followed wallets' trades from Polygon exchange fill logs.

Fills are pushed over a WebSocket ``logs`` subscription, with ``newHeads`` on the same
connection caching block timestamps so a pushed fill needs no RPC round trip. An
``eth_getLogs`` backstop walks blocks from a persisted cursor: every
``backstop_interval_s`` while the stream is up, every ``poll_interval_s`` while it is down.
A restart resumes from the cursor, so outages are replayed rather than skipped.

Each fill becomes a ``source="chain"`` row on the trade tape and is handed to the copy
engine. A fill whose token Gamma cannot map yet is retried for ``unmapped_ttl`` before it
is reported and dropped. Logs flagged ``removed`` (reorgs) are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import websockets
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import FeedCursor, WatchedWallet
from app.domain.events import ActivityType, SourceActivity, assign_event_keys
from app.domain.market import MarketInfo
from app.engine.copy_engine import CopyEngine
from app.feeds.tape import TradeTape
from app.integrations.polygon import ChainFill, PolygonRpc, decode_fill, fill_filter
from app.integrations.polymarket.markets import MarketMetadataClient
from app.services.event_bus import EventBus
from app.services.system_events import SystemEventLog
from app.services.wallets import WalletRegistry

log = logging.getLogger(__name__)

CURSOR = "polygon_fills"


class ChainFeed:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        rpc: PolygonRpc,
        meta: MarketMetadataClient,
        registry: WalletRegistry,
        tape: TradeTape,
        engine: CopyEngine,
        events: SystemEventLog,
        bus: EventBus,
        clock: Clock,
        *,
        ws_url: str | None = None,
        poll_interval_s: float = 0.5,
        backstop_interval_s: float = 2.0,
        lookback_blocks: int = 30,
        max_block_range: int = 200,
        unmapped_ttl: timedelta = timedelta(minutes=10),
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self._sm = sm
        self._rpc = rpc
        self._meta = meta
        self._registry = registry
        self._tape = tape
        self._engine = engine
        self._events = events
        self._bus = bus
        self._clock = clock
        self._ws_url = ws_url
        self._interval = poll_interval_s
        self._backstop_interval = backstop_interval_s
        self._lookback = lookback_blocks
        self._max_range = max_block_range
        self._unmapped_ttl = unmapped_ttl
        self.on_tick = on_tick
        self._next_block: int | None = None
        self._tokens: dict[str, MarketInfo] = {}
        self._unmapped: dict[tuple[str, int], tuple[ChainFill, datetime]] = {}
        self._wallets: dict[str, WatchedWallet] = {}
        self._stream_wallets: frozenset[str] = frozenset()
        self._resubscribe = asyncio.Event()
        self.stream_connected = False
        self.stream_reconnects = 0
        self.last_tick_at: datetime | None = None
        self.last_lag_s: float | None = None
        """Detection minus block time for the most recent new fill."""

    # ------------------------------------------------------------------ lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        stream = asyncio.create_task(self._stream_forever(stop)) if self._ws_url else None
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # RPC/network hiccups must not kill the feed
                    log.warning("chain feed tick failed", extra={"error": repr(exc)})
                if self.on_tick:
                    self.on_tick()
                interval = self._backstop_interval if self.stream_connected else self._interval
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval)
        finally:
            if stream is not None:
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream

    async def _stream_forever(self, stop: asyncio.Event) -> None:
        assert self._ws_url is not None
        attempt = 0
        while not stop.is_set():
            makers = sorted(self._wallets)
            if not makers:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
                continue
            try:
                async with websockets.connect(
                    self._ws_url, open_timeout=10, ping_interval=20, max_size=None
                ) as ws:
                    self._resubscribe.clear()
                    for i, params in enumerate((["newHeads"], ["logs", fill_filter(makers)])):
                        await ws.send(
                            json.dumps(
                                {"jsonrpc": "2.0", "id": i + 1, "method": "eth_subscribe",
                                 "params": params}
                            )
                        )
                    self._stream_wallets = frozenset(makers)
                    self.stream_connected = True
                    attempt = 0
                    async for raw in ws:
                        if self._resubscribe.is_set():
                            break  # followed wallets changed: reconnect with the new filter
                        await self.handle_message(json.loads(raw))
            except Exception as exc:  # network or protocol errors: back off and reconnect
                log.warning("chain stream disconnected", extra={"error": repr(exc)})
            finally:
                if self.stream_connected:
                    self.stream_reconnects += 1
                self.stream_connected = False
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(30, 0.5 * 2**attempt))

    # ------------------------------------------------------------------ inputs
    async def handle_message(self, msg: dict[str, Any]) -> int:
        """Apply one subscription message; returns new trades ingested (0 for heads/acks)."""
        if msg.get("method") != "eth_subscription":
            return 0
        result = msg["params"]["result"]
        if "logIndex" in result:
            if result.get("removed"):
                return 0
            fill = decode_fill(result)
            wallet = self._wallets.get(fill.maker) if fill else None
            if fill is None or wallet is None:
                return 0
            return await self._ingest(wallet, [fill])
        if "number" in result and "timestamp" in result:
            self._rpc.remember_block(int(result["number"], 16), int(result["timestamp"], 16))
        return 0

    async def tick(self) -> int:
        """Backstop pass: walk new blocks from the cursor; returns new trades ingested."""
        self._wallets = {w.address.lower(): w for w in await self._registry.active()}
        if self.stream_connected and frozenset(self._wallets) != self._stream_wallets:
            self._resubscribe.set()
        head = await self._rpc.block_number()
        if self._next_block is None:
            self._next_block = await self._load_cursor(head)
        ingested = await self._retry_unmapped()
        if head >= self._next_block:
            to_block = min(head, self._next_block + self._max_range - 1)
            fills = await self._rpc.fills(self._next_block, to_block, list(self._wallets))
            by_wallet: dict[str, list[ChainFill]] = {}
            for f in fills:
                by_wallet.setdefault(f.maker, []).append(f)
            for address, wallet_fills in by_wallet.items():
                ingested += await self._ingest(self._wallets[address], wallet_fills)
            await self._save_cursor(to_block + 1)
        self.last_tick_at = self._clock.now()
        return ingested

    # ------------------------------------------------------------------ ingestion
    async def _ingest(self, wallet: WatchedWallet, fills: list[ChainFill]) -> int:
        rows: list[SourceActivity] = []
        for f in fills:
            row = await self._to_activity(wallet, f)
            if row is None:
                self._unmapped.setdefault((f.tx_hash, f.log_index), (f, self._clock.now()))
                continue
            self._unmapped.pop((f.tx_hash, f.log_index), None)
            rows.append(row)
        if not rows:
            return 0
        detected_at = self._clock.now()
        new = await self._tape.ingest(
            wallet.id,
            assign_event_keys(rows),
            detected_at=detected_at,
            followed_since=wallet.followed_since,
            source="chain",
        )
        if not new:
            return 0
        live = [ev for ev in new if not ev.is_backfill]
        for ev in live:
            self._bus.publish("source_trade", {"id": ev.id})
            self.last_lag_s = (detected_at - ev.block_ts).total_seconds()
        if live:
            await self._touch_last_trade(wallet.id, max(ev.block_ts for ev in live))
        for ev in new:
            await self._engine.on_trade(ev, wallet)
        self._bus.publish("wallet", {"id": wallet.id})
        return len(new)

    async def _to_activity(self, wallet: WatchedWallet, f: ChainFill) -> SourceActivity | None:
        market = await self._market_for(f.token_id)
        token = market.token(f.token_id) if market else None
        if market is None or token is None:
            return None
        ts = await self._rpc.block_timestamp(f.block_number)
        return SourceActivity(
            wallet=wallet.address.lower(),
            type=ActivityType.TRADE,
            raw_type="TRADE",
            side=f.side,
            condition_id=market.condition_id,
            token_id=f.token_id,
            outcome=token.outcome,
            outcome_index=token.outcome_index,
            size=f.shares,
            usdc_size=f.usdc,
            price=f.price,
            tx_hash=f.tx_hash,
            block_ts=datetime.fromtimestamp(ts, tz=UTC),
            title=market.question,
            slug=market.slug,
        )

    async def _retry_unmapped(self) -> int:
        ingested = 0
        now = self._clock.now()
        for key, (fill, first_seen) in list(self._unmapped.items()):
            wallet = self._wallets.get(fill.maker)
            if wallet is None or now - first_seen > self._unmapped_ttl:
                self._unmapped.pop(key, None)
                if wallet is not None:
                    await self._events.record(
                        "UNMAPPED_FILL_DROPPED",
                        f"Token {fill.token_id} in {fill.tx_hash} never mapped to a market",
                        level="WARNING",
                        data={"wallet": wallet.address, "tx": fill.tx_hash},
                    )
                continue
            ingested += await self._ingest(wallet, [fill])
        return ingested

    async def _market_for(self, token_id: str) -> MarketInfo | None:
        market = self._tokens.get(token_id)
        if market is None:
            market = await self._meta.fetch_by_token(token_id)
            if market is not None:
                for t in market.tokens:
                    self._tokens[t.token_id] = market
        return market

    # ------------------------------------------------------------------ persistence
    async def _load_cursor(self, head: int) -> int:
        async with self._sm() as s:
            row = await s.get(FeedCursor, CURSOR)
        return row.next_block if row is not None else max(0, head - self._lookback)

    async def _save_cursor(self, next_block: int) -> None:
        self._next_block = next_block
        now = self._clock.now()
        async with self._sm() as s, s.begin():
            await s.execute(
                pg_insert(FeedCursor)
                .values(name=CURSOR, next_block=next_block, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["name"], set_={"next_block": next_block, "updated_at": now}
                )
            )

    async def _touch_last_trade(self, wallet_id: int, block_ts: datetime) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(
                update(WatchedWallet)
                .where(
                    WatchedWallet.id == wallet_id,
                    (WatchedWallet.last_trade_at.is_(None))
                    | (WatchedWallet.last_trade_at < block_ts),
                )
                .values(last_trade_at=block_ts)
            )
