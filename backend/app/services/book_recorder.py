"""Records executable order books of relevant tokens so replays have real, timestamped prices."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import BookSnapshot, Lot, SourceEvent, WatchedWallet
from app.domain.market import OrderBook
from app.services.market_data import MarketDataService

log = logging.getLogger(__name__)

MAX_TOKENS = 200


class BookRecorder:
    def __init__(
        self, sm: async_sessionmaker[AsyncSession], market_data: MarketDataService, clock: Clock
    ) -> None:
        self._sm = sm
        self._md = market_data
        self._clock = clock

    async def tokens_of_interest(self) -> list[str]:
        since = self._clock.now() - timedelta(hours=24)
        async with self._sm() as s:
            held = set(
                (
                    await s.scalars(select(Lot.token_id).where(Lot.shares_remaining > 0).distinct())
                ).all()
            )
            traded = (
                await s.execute(
                    select(SourceEvent.token_id, func.max(SourceEvent.block_ts).label("ts"))
                    .join(WatchedWallet, WatchedWallet.id == SourceEvent.wallet_id)
                    .where(
                        WatchedWallet.removed_at.is_(None),
                        SourceEvent.type == "TRADE",
                        SourceEvent.block_ts >= since,
                        SourceEvent.token_id != "",
                    )
                    .group_by(SourceEvent.token_id)
                    .order_by(func.max(SourceEvent.block_ts).desc())
                    .limit(MAX_TOKENS)
                )
            ).all()
        tokens = list(held) + [t for t, _ in traded if t not in held]
        return tokens[:MAX_TOKENS]

    async def record(self, books: dict[str, OrderBook]) -> int:
        rows = []
        for token, b in books.items():
            rows.append(
                BookSnapshot(
                    token_id=token,
                    condition_id=b.condition_id,
                    received_at=b.received_at,
                    bids={"levels": [[str(lv.price), str(lv.size)] for lv in b.bids]},
                    asks={
                        "levels": [[str(lv.price), str(lv.size)] for lv in b.asks],
                        "market": (
                            info.model_dump(mode="json")
                            if (info := await self._md.get_market(b.condition_id)) is not None
                            else None
                        ),
                    },
                    tick_size=b.tick_size,
                    min_order_size=b.min_order_size,
                    hash=b.hash,
                )
            )
        if rows:
            async with self._sm() as s, s.begin():
                s.add_all(rows)
        return len(rows)

    async def tick(self) -> None:
        tokens = await self.tokens_of_interest()
        if not tokens:
            return
        books = await self._md.fetch_books(tokens)
        complements = set()
        for b in books.values():
            info = await self._md.get_market(b.condition_id)
            if info is not None and info.complement_verified:
                complements.update(t.token_id for t in info.tokens)
        missing = sorted(complements.difference(books))
        if missing:
            books.update(await self._md.fetch_books(missing))
        await self.record(books)
