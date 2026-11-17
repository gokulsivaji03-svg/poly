"""Market history for replay when nothing was recorded locally.

Three public sources, loaded once before a run so the replay itself is deterministic:

* the wallet's activity from the Data API;
* market metadata from Gamma, with ``closed_at`` and on-chain payouts for settlement;
* CLOB ``/prices-history`` one-minute bars for every traded token.

A minute bar is a price, not an order book. A copy fills at the last bar at or before its
detection time, as if the whole order traded at that price: no spread, no depth, no impact.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.money import to_decimal
from app.domain.events import SourceActivity
from app.domain.market import BookLevel, MarketInfo, OrderBook
from app.integrations.http import ResilientHttp, UpstreamError
from app.integrations.polymarket.data_api import DataApiClient
from app.integrations.polymarket.markets import MarketMetadataClient

log = logging.getLogger(__name__)

PRICE_CHUNK = timedelta(days=7)
"""``/prices-history`` rejects longer startTs..endTs spans."""
BAR_IS_CURRENT = timedelta(seconds=90)
"""A one-minute bar stands for the price until the next one, with slack for a late print."""
DEPTH = Decimal(1_000_000)
"""Shares offered at a bar's price: minute bars carry no depth, so depth never binds."""
CONCURRENCY = 8

Bar = tuple[datetime, Decimal]


@dataclass(frozen=True, slots=True)
class MarketHistory:
    markets: dict[str, MarketInfo] = field(default_factory=dict)
    bars: dict[str, list[Bar]] = field(default_factory=dict)

    def market_at(self, condition_id: str, now: datetime) -> MarketInfo | None:
        """The market as it stood at ``now``: open until it closed, settled after it resolved."""
        info = self.markets.get(condition_id)
        if info is None:
            return None
        closed_at = info.closed_at or info.end_date
        over = closed_at is not None and closed_at <= now
        return info.model_copy(
            update={
                "active": True,
                "closed": over,
                "accepting_orders": not over,
                "resolved": over and info.resolved,
                "payouts": info.payouts if over else {},
                "fetched_at": now,
            }
        )

    def book_at(self, token_id: str, condition_id: str, now: datetime) -> OrderBook | None:
        """A one-level book at the last bar at or before ``now``; stale once the bar is old."""
        series = self.bars.get(token_id)
        info = self.markets.get(condition_id)
        if not series or info is None:
            return None
        i = bisect.bisect_right(series, now, key=lambda b: b[0])
        if i == 0:
            return None
        at, price = series[i - 1]
        tick = info.tick_size
        bid = price - tick
        return OrderBook(
            token_id=token_id,
            condition_id=condition_id,
            asks=(BookLevel(price=price, size=DEPTH),),
            bids=(BookLevel(price=bid, size=DEPTH),) if bid > 0 else (),
            tick_size=tick,
            min_order_size=info.min_order_size,
            received_at=now if now - at <= BAR_IS_CURRENT else at,
            hash=f"bar:{int(at.timestamp())}",
        )


def equity_path(
    anchor_equity: Decimal,
    anchor_at: datetime,
    pnl: list[Bar],
    start: datetime,
    until: datetime,
) -> list[Bar]:
    """A wallet's equity at each P&L point in ``[start, until)``, walked back from one
    observation: equity(t) = anchor - (P&L at anchor - P&L at t). Assumes no deposits or
    withdrawals in between; points that would leave the wallet broke are dropped."""
    i = bisect.bisect_right(pnl, anchor_at, key=lambda b: b[0])
    if i == 0:
        return []
    at_anchor = pnl[i - 1][1]
    path = [(t, anchor_equity - (at_anchor - v)) for t, v in pnl if start <= t < until]
    return [(t, e) for t, e in path if e > 0]


class HistoryClient:
    def __init__(
        self,
        http: ResilientHttp,
        data_api: DataApiClient,
        metadata: MarketMetadataClient,
        clob_url: str,
    ) -> None:
        self._http = http
        self._data = data_api
        self._meta = metadata
        self._clob = clob_url.rstrip("/")

    async def pnl(self, address: str) -> list[Bar]:
        try:
            return await self._data.pnl_history(address)
        except UpstreamError as exc:
            log.warning("replay pnl history unavailable", extra={"error": str(exc)})
            return []

    async def activity(self, address: str, start: datetime, end: datetime) -> list[SourceActivity]:
        batch = await self._data.activity_since(address, start)
        if batch.truncated:
            raise ValueError(f"activity for {address} exceeds the page limit; shorten the window")
        return [r for r in batch.rows if r.block_ts <= end]

    async def load(self, tokens: dict[str, str], start: datetime, end: datetime) -> MarketHistory:
        """Metadata and minute bars for ``tokens`` (token id to condition id) over the window."""
        sem = asyncio.Semaphore(CONCURRENCY)

        async def market(cid: str) -> tuple[str, MarketInfo | None]:
            async with sem:
                try:
                    return cid, await self._meta.fetch(cid)
                except UpstreamError as exc:
                    log.warning(
                        "replay market unavailable", extra={"condition": cid, "error": str(exc)}
                    )
                    return cid, None

        async def bars(token: str) -> tuple[str, list[Bar]]:
            async with sem:
                return token, await self._bars(token, start, end)

        markets = await asyncio.gather(*(market(c) for c in set(tokens.values())))
        series = await asyncio.gather(*(bars(t) for t in tokens))
        return MarketHistory(
            markets={c: m for c, m in markets if m is not None},
            bars={t: s for t, s in series if s},
        )

    async def _bars(self, token: str, start: datetime, end: datetime) -> list[Bar]:
        out: dict[int, Decimal] = {}
        t = start
        while t < end:
            stop = min(t + PRICE_CHUNK, end)
            try:
                body = await self._http.get_json(
                    f"{self._clob}/prices-history",
                    {
                        "market": token,
                        "startTs": int(t.timestamp()),
                        "endTs": int(stop.timestamp()),
                        "fidelity": 1,
                    },
                )
            except UpstreamError as exc:
                log.warning("replay prices unavailable", extra={"token": token, "error": str(exc)})
                return []
            for pt in body.get("history") or []:
                out[int(pt["t"])] = to_decimal(pt["p"])
            t = stop
        return [(datetime.fromtimestamp(ts, UTC), p) for ts, p in sorted(out.items())]
