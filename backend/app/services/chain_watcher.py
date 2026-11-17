"""ChainFillWatcher: detect followed wallets' trades from Polygon exchange logs (~0.1 s).

The Data API indexes trades several seconds after the block; exchange fill logs are
available as soon as the block is. Fills arrive over a WebSocket ``logs`` subscription
(push), with ``newHeads`` on the same connection caching block timestamps so a fill needs
no extra round trip. An ``eth_getLogs`` poll runs as a backstop: every
``backstop_interval_s`` while the stream is up, every ``poll_interval_s`` when it is down,
so a dropped connection or a missed message is recovered from the block cursor.

Each fill becomes a ``source="chain"`` activity row and goes through the same copy pipeline.
The Data API poller keeps running as the reconciling source; ``TradeEventStore`` drops
whichever copy of a trade arrives second, so nothing is copied twice.

A fill whose token cannot be mapped to a market yet is left to the Data API path rather
than stored with missing metadata (a stored chain row would block the complete API row).
Logs flagged ``removed`` (reorg) are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import websockets

from app.core.clock import Clock
from app.db.models import WatchedWallet
from app.domain.events import ActivityType, SourceActivity, assign_event_keys
from app.domain.market import MarketInfo
from app.integrations.polygon import ChainFill, PolygonRpc, decode_fill, fill_filter
from app.integrations.polymarket.markets import MarketMetadataClient
from app.services.copy_signal import CopySignalService
from app.services.event_bus import EventBus
from app.services.event_store import TradeEventStore
from app.services.wallets import WalletRegistry

log = logging.getLogger(__name__)


class ChainFillWatcher:
    def __init__(
        self,
        rpc: PolygonRpc,
        meta: MarketMetadataClient,
        registry: WalletRegistry,
        store: TradeEventStore,
        copy: CopySignalService,
        bus: EventBus,
        clock: Clock,
        *,
        poll_interval_s: float = 0.5,
        backstop_interval_s: float = 2.0,
        lookback_blocks: int = 30,
        max_block_range: int = 200,
        ws_url: str | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self._rpc = rpc
        self._meta = meta
        self._registry = registry
        self._store = store
        self._copy = copy
        self._bus = bus
        self._clock = clock
        self._interval = poll_interval_s
        self._backstop_interval = backstop_interval_s
        self._lookback = lookback_blocks
        self._max_range = max_block_range
        self._ws_url = ws_url
        self.on_tick = on_tick
        self._next_block: int | None = None
        self._tokens: dict[str, MarketInfo] = {}
        self._wallets: dict[str, WatchedWallet] = {}
        self._stream_wallets: frozenset[str] = frozenset()
        self._resubscribe = asyncio.Event()
        self.stream_connected = False
        self.stream_reconnects = 0
        self.last_lag_s: float | None = None
        """Detection minus block time for the most recent copied fill."""

    async def run(self, stop: asyncio.Event) -> None:
        stream = asyncio.create_task(self._stream_forever(stop)) if self._ws_url else None
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # RPC/network hiccups must not kill the worker
                    log.warning("chain watcher tick failed", extra={"error": repr(exc)})
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
                        await ws.send(json.dumps(
                            {"jsonrpc": "2.0", "id": i + 1, "method": "eth_subscribe",
                             "params": params}
                        ))
                    self._stream_wallets = frozenset(makers)
                    self.stream_connected = True
                    attempt = 0
                    async for raw in ws:
                        if self._resubscribe.is_set():
                            break  # followed wallets changed: reconnect with the new filter
                        await self.handle_message(json.loads(raw))
            except Exception as exc:  # network errors, protocol errors: back off, reconnect
                log.warning("chain stream disconnected", extra={"error": repr(exc)})
            finally:
                if self.stream_connected:
                    self.stream_reconnects += 1
                self.stream_connected = False
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(30, 0.5 * 2**attempt))

    async def handle_message(self, msg: dict[str, Any]) -> int:
        """Apply one subscription message; returns fills ingested (0 for heads/acks)."""
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
        """Process new blocks once; returns the number of copy-eligible fills ingested."""
        head = await self._rpc.block_number()
        if self._next_block is None:
            self._next_block = max(0, head - self._lookback)
        if head < self._next_block:
            return 0
        to_block = min(head, self._next_block + self._max_range - 1)
        self._wallets = {w.address.lower(): w for w in await self._registry.active()}
        if self.stream_connected and frozenset(self._wallets) != self._stream_wallets:
            self._resubscribe.set()
        wallets = self._wallets
        fills = await self._rpc.fills(self._next_block, to_block, list(wallets))
        by_wallet: dict[str, list[ChainFill]] = {}
        for f in fills:
            by_wallet.setdefault(f.maker, []).append(f)
        ingested = 0
        for address, wallet_fills in by_wallet.items():
            ingested += await self._ingest(wallets[address], wallet_fills)
        self._next_block = to_block + 1
        return ingested

    async def _ingest(self, wallet: WatchedWallet, fills: list[ChainFill]) -> int:
        rows: list[SourceActivity] = []
        for f in fills:
            market = await self._market_for(f.token_id)
            token = market.token(f.token_id) if market else None
            if market is None or token is None:
                continue  # unmapped yet: the Data API row will carry full metadata
            ts = await self._rpc.block_timestamp(f.block_number)
            rows.append(
                SourceActivity(
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
            )
        if not rows:
            return 0
        detected_at = self._clock.now()
        new = await self._store.ingest(
            wallet.id,
            assign_event_keys(rows),
            detected_at=detected_at,
            followed_since=wallet.followed_since,
            source="chain",
        )
        for ev in new:
            if not ev.is_backfill:
                self._bus.publish("source_trade", {"id": ev.id})
                self.last_lag_s = (detected_at - ev.block_ts).total_seconds()
        for ev in new:
            await self._copy.process(ev, wallet)
        if new:
            self._bus.publish("wallet", {"id": wallet.id})
        return len(new)

    async def _market_for(self, token_id: str) -> MarketInfo | None:
        market = self._tokens.get(token_id)
        if market is None:
            market = await self._meta.fetch_by_token(token_id)
            if market is not None:
                for t in market.tokens:
                    self._tokens[t.token_id] = market
        return market
