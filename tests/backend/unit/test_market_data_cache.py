"""Market metadata cache: a copy never waits on Gamma for a market it has already seen."""

import asyncio
from datetime import timedelta

from app.core.clock import ManualClock
from app.services.market_data import MarketDataService

from factories import COND, NOW, market


class CountingMeta:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0

    async def fetch(self, condition_id):
        self.calls += 1
        return market().model_copy(update={"fetched_at": self.clock.now()})


class NoBooks:
    async def fetch(self, token_ids):
        return {}


def service():
    clock = ManualClock(NOW)
    meta = CountingMeta(clock)
    return MarketDataService(meta, NoBooks(), clock), meta, clock


async def test_fresh_metadata_is_served_from_cache():
    md, meta, clock = service()
    await md.get_market(COND)
    clock.advance(60)
    await md.get_market(COND)
    assert meta.calls == 1


async def test_stale_metadata_is_returned_at_once_and_refreshed_behind():
    md, meta, clock = service()
    first = await md.get_market(COND)
    clock.advance(6 * 60)
    served = await md.get_market(COND)
    assert served is first and meta.calls == 1, "returned without waiting on a fetch"
    await asyncio.sleep(0)
    assert meta.calls == 2, "a background refresh ran"
    refreshed = await md.get_market(COND)
    assert refreshed is not None and refreshed.fetched_at == clock.now()


async def test_metadata_past_the_stale_limit_is_fetched_before_use():
    md, meta, clock = service()
    await md.get_market(COND)
    clock.advance(timedelta(minutes=31).total_seconds())
    got = await md.get_market(COND)
    assert meta.calls == 2 and got is not None and got.fetched_at == clock.now()
