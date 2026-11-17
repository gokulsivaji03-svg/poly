"""Warm metadata for newly listed markets before anyone trades them.

A followed wallet's first fill in a market otherwise waits on Gamma twice (token to market
in the feed, then validated metadata in the context load), measured at 182 ms p50 per call.
Polling the newest listings maps their tokens in the feed and caches validated metadata,
without subscribing their books, so the market stream does not grow with every listing.
"""

from __future__ import annotations

import asyncio

from app.feeds.chain import ChainFeed
from app.integrations.polymarket.markets import MarketMetadataClient
from app.services.market_data import MarketDataService


class MarketCatalog:
    def __init__(
        self,
        meta: MarketMetadataClient,
        feed: ChainFeed,
        market_data: MarketDataService,
        *,
        page: int = 100,
        concurrency: int = 4,
    ) -> None:
        self._meta = meta
        self._feed = feed
        self._md = market_data
        self._page = page
        self._sem = asyncio.Semaphore(concurrency)
        self._seen: set[str] = set()

    async def tick(self) -> int:
        """Warm listings not seen before; returns how many."""
        new = [m for m in await self._meta.recent(self._page) if m.condition_id not in self._seen]
        for market in new:
            self._feed.remember_market(market)

        async def warm(condition_id: str) -> None:
            async with self._sem:
                await self._md.get_market(condition_id, watch=False)

        await asyncio.gather(*(warm(m.condition_id) for m in new), return_exceptions=True)
        self._seen.update(m.condition_id for m in new)
        return len(new)
