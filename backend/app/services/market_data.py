"""MarketDataService: market metadata cache, executable books, and marks.

Books come from the live WebSocket stream when it is connected and fresh; otherwise from a
REST snapshot. ``fresh=True`` (used right before execution) only accepts a book younger than
``fresh_max_age``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from app.core.clock import Clock
from app.domain.market import MarketInfo, OrderBook
from app.integrations.http import UpstreamError

log = logging.getLogger(__name__)


class MarketMetadataSource(Protocol):
    async def fetch(self, condition_id: str) -> MarketInfo | None: ...


class RestBookSource(Protocol):
    async def fetch(self, token_ids: list[str]) -> dict[str, OrderBook]: ...


class LiveBookSource(Protocol):
    connected: bool

    def watch(self, token_ids: Iterable[str]) -> None: ...

    def book(self, token_id: str) -> OrderBook | None: ...


@dataclass(frozen=True, slots=True)
class Mark:
    price: Decimal | None
    source: str
    best_bid: Decimal | None
    best_ask: Decimal | None


class MarketDataService:
    def __init__(
        self,
        meta: MarketMetadataSource,
        rest: RestBookSource,
        clock: Clock,
        live: LiveBookSource | None = None,
        *,
        market_ttl: timedelta = timedelta(minutes=5),
        book_ttl: timedelta = timedelta(seconds=5),
        fresh_max_age: timedelta = timedelta(seconds=2),
    ) -> None:
        self._meta = meta
        self._rest = rest
        self._live = live
        self._clock = clock
        self._markets: dict[str, MarketInfo] = {}
        self._books: dict[str, OrderBook] = {}
        self._market_ttl = market_ttl
        self._book_ttl = book_ttl
        self._fresh_max_age = fresh_max_age
        self._locks: dict[str, asyncio.Lock] = {}
        self.book_fetch_errors = 0

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def get_market(self, condition_id: str, *, refresh: bool = False) -> MarketInfo | None:
        cached = self._markets.get(condition_id)
        now = self._clock.now()
        if cached and not refresh and now - cached.fetched_at < self._market_ttl:
            return cached
        async with self._lock(f"m:{condition_id}"):
            cached = self._markets.get(condition_id)
            if cached and not refresh and now - cached.fetched_at < self._market_ttl:
                return cached
            try:
                info = await self._meta.fetch(condition_id)
            except UpstreamError as exc:
                log.warning(
                    "market metadata unavailable",
                    extra={"condition": condition_id, "error": str(exc)},
                )
                return cached  # stale metadata beats none; callers check fetched_at
            if info is not None:
                self._markets[condition_id] = info
                if self._live is not None:
                    self._live.watch(t.token_id for t in info.tokens)
            return info

    def watch(self, token_ids: Iterable[str]) -> None:
        if self._live is not None:
            self._live.watch(token_ids)

    async def get_book(self, token_id: str, *, fresh: bool = False) -> OrderBook | None:
        now = self._clock.now()
        max_age = self._fresh_max_age if fresh else self._book_ttl
        if self._live is not None and self._live.connected:
            live = self._live.book(token_id)
            if live is not None and live.age(now) <= max_age:
                return live
        cached = self._books.get(token_id)
        if cached is not None and cached.age(now) <= max_age:
            return cached
        books = await self.fetch_books([token_id])
        return books.get(token_id)

    async def fetch_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        if not token_ids:
            return {}
        try:
            books = await self._rest.fetch(token_ids)
        except (UpstreamError, OSError) as exc:
            self.book_fetch_errors += 1
            log.warning("book fetch failed", extra={"tokens": len(token_ids), "error": str(exc)})
            return {}
        except Exception as exc:  # SDK raises its own error hierarchy
            self.book_fetch_errors += 1
            log.warning("book fetch failed", extra={"tokens": len(token_ids), "error": repr(exc)})
            return {}
        self._books.update(books)
        return books

    async def marks(self, token_ids: Iterable[str]) -> dict[str, Mark]:
        tokens = sorted(set(token_ids))
        now = self._clock.now()
        missing = [
            t
            for t in tokens
            if (b := self._best_cached(t)) is None or b.age(now) > self._book_ttl * 6
        ]
        if missing:
            await self.fetch_books(missing)
        out: dict[str, Mark] = {}
        for t in tokens:
            b = self._best_cached(t)
            if b is None:
                out[t] = Mark(None, "none", None, None)
                continue
            price, source = b.mark_price()
            out[t] = Mark(price, source, b.best_bid, b.best_ask)
        return out

    def _best_cached(self, token_id: str) -> OrderBook | None:
        live = (
            self._live.book(token_id) if self._live is not None and self._live.connected else None
        )
        rest = self._books.get(token_id)
        if live and rest:
            return live if live.received_at >= rest.received_at else rest
        return live or rest
