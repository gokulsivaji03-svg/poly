"""Order books from the CLOB: REST snapshots via the official SDK, live deltas via WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import websockets

from app.core.clock import Clock
from app.core.money import ZERO, to_decimal
from app.domain.market import BookLevel, OrderBook
from app.integrations.http import backoff_delay

log = logging.getLogger(__name__)


class SdkBookClient(Protocol):
    async def get_order_books(self, *, token_ids: list[str]) -> Any: ...


def _levels(raw: Iterable[Any]) -> tuple[BookLevel, ...]:
    out: list[BookLevel] = []
    for lv in raw:
        price = to_decimal(getattr(lv, "price", None) if not isinstance(lv, dict) else lv["price"])
        size = to_decimal(getattr(lv, "size", None) if not isinstance(lv, dict) else lv["size"])
        if size > ZERO and ZERO < price < Decimal(1):
            out.append(BookLevel(price=price, size=size))
    return tuple(out)


class ClobRestBooks:
    """Batch book snapshots through ``polymarket-client``'s ``AsyncPublicClient``."""

    CHUNK = 50

    def __init__(self, sdk: SdkBookClient, clock: Clock) -> None:
        self._sdk = sdk
        self._clock = clock

    async def fetch(self, token_ids: list[str]) -> dict[str, OrderBook]:
        out: dict[str, OrderBook] = {}
        for i in range(0, len(token_ids), self.CHUNK):
            chunk = token_ids[i : i + self.CHUNK]
            books = await self._sdk.get_order_books(token_ids=chunk)
            received = self._clock.now()
            for b in books:
                ts = getattr(b, "timestamp", None)
                out[str(b.asset_id)] = OrderBook(
                    token_id=str(b.asset_id),
                    condition_id=str(getattr(b, "condition_id", None) or b.market),
                    bids=_levels(b.bids),
                    asks=_levels(b.asks),
                    tick_size=to_decimal(b.tick_size)
                    if b.tick_size is not None
                    else Decimal("0.01"),
                    min_order_size=to_decimal(b.min_order_size)
                    if b.min_order_size is not None
                    else ZERO,
                    exchange_ts=ts if isinstance(ts, datetime) else None,
                    received_at=received,
                    hash=getattr(b, "hash", None),
                )
        return out


@dataclass
class _LiveBook:
    condition_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    received_at: datetime | None = None
    hash: str | None = None
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = ZERO
    snapshot_seen: bool = False


class MarketStream:
    """Maintains live books for subscribed tokens from the public market channel.

    Deltas are only applied after a full ``book`` snapshot for that token, and every
    reconnect clears state and resubscribes, so a gap can never leave a silently wrong book.
    """

    def __init__(
        self, url: str, clock: Clock, *, on_update: Callable[[str], None] | None = None
    ) -> None:
        self._url = url
        self._clock = clock
        self._on_update = on_update
        self._tokens: set[str] = set()
        self._books: dict[str, _LiveBook] = {}
        self._resubscribe = asyncio.Event()
        self.connected = False
        self.reconnects = 0
        self.last_message_at: datetime | None = None

    def watch(self, token_ids: Iterable[str]) -> None:
        new = set(token_ids) - self._tokens
        if new:
            self._tokens |= new
            self._resubscribe.set()

    def book(self, token_id: str) -> OrderBook | None:
        lb = self._books.get(token_id)
        if lb is None or not lb.snapshot_seen or lb.received_at is None or not self.connected:
            return None
        return OrderBook(
            token_id=token_id,
            condition_id=lb.condition_id,
            bids=tuple(BookLevel(price=p, size=s) for p, s in lb.bids.items()),
            asks=tuple(BookLevel(price=p, size=s) for p, s in lb.asks.items()),
            tick_size=lb.tick_size,
            min_order_size=lb.min_order_size,
            received_at=lb.received_at,
            hash=lb.hash,
        )

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            if not self._tokens:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._resubscribe.wait(), timeout=5)
                continue
            try:
                async with websockets.connect(self._url, ping_interval=None, open_timeout=10) as ws:
                    self._books.clear()
                    self._resubscribe.clear()
                    await ws.send(
                        json.dumps({"assets_ids": sorted(self._tokens), "type": "market"})
                    )
                    self.connected = True
                    attempt = 0
                    await self._pump(ws, stop)
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                log.warning("market ws disconnected", extra={"error": str(exc)})
            finally:
                if self.connected:
                    self.reconnects += 1
                self.connected = False
            if stop.is_set():
                break
            await asyncio.sleep(backoff_delay(attempt, base=1.0, cap=30.0))
            attempt += 1

    async def _pump(self, ws: Any, stop: asyncio.Event) -> None:
        last_ping = self._clock.now()
        while not stop.is_set() and not self._resubscribe.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except TimeoutError:
                raw = None
            now = self._clock.now()
            if (now - last_ping).total_seconds() >= 10:
                await ws.send("PING")
                last_ping = now
            if raw is None or raw == "PONG":
                continue
            self.last_message_at = now
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for event in msg if isinstance(msg, list) else [msg]:
                self.apply(event, now)

    def apply(self, event: dict[str, Any], now: datetime) -> None:
        kind = event.get("event_type")
        if kind == "book":
            token = str(event.get("asset_id"))
            lb = _LiveBook(condition_id=str(event.get("market", "")))
            lb.bids = {
                to_decimal(lv["price"]): to_decimal(lv["size"])
                for lv in event.get("bids", [])
                if to_decimal(lv["size"]) > ZERO
            }
            lb.asks = {
                to_decimal(lv["price"]): to_decimal(lv["size"])
                for lv in event.get("asks", [])
                if to_decimal(lv["size"]) > ZERO
            }
            lb.received_at = now
            lb.hash = event.get("hash")
            lb.snapshot_seen = True
            prev = self._books.get(token)
            if prev is not None:
                lb.tick_size, lb.min_order_size = prev.tick_size, prev.min_order_size
            self._books[token] = lb
            self._notify(token)
        elif kind == "price_change":
            for ch in event.get("price_changes", []):
                token = str(ch.get("asset_id"))
                lb = self._books.get(token)
                if lb is None or not lb.snapshot_seen:
                    continue  # never apply deltas without a base snapshot
                side = lb.bids if str(ch.get("side")).upper() == "BUY" else lb.asks
                price, size = to_decimal(ch["price"]), to_decimal(ch["size"])
                if size <= ZERO:
                    side.pop(price, None)
                else:
                    side[price] = size
                lb.received_at = now
                lb.hash = ch.get("hash", lb.hash)
                self._notify(token)
        elif kind == "tick_size_change":
            lb = self._books.get(str(event.get("asset_id")))
            if lb is not None and event.get("new_tick_size") is not None:
                lb.tick_size = to_decimal(event["new_tick_size"])

    def _notify(self, token: str) -> None:
        if self._on_update is not None:
            self._on_update(token)
