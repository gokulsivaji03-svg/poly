"""TradeEventStore: idempotent persistence of public wallet activity."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SourceEvent
from app.domain.events import SourceActivity

INSERT_CHUNK = 1000


class TradeEventStore:
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm

    async def ingest(
        self,
        wallet_id: int,
        rows: Sequence[SourceActivity],
        *,
        detected_at: datetime,
        followed_since: datetime,
    ) -> list[SourceEvent]:
        """Insert unseen rows; return only the newly inserted ones, oldest first.

        Duplicates (repeated API rows, overlap re-reads) collide on ``event_key`` and are
        silently ignored, so a repeated event can never be mistaken for a new trade.
        """
        if not rows:
            return []
        values = [
            {
                "wallet_id": wallet_id,
                "event_key": r.event_key,
                "occurrence": r.occurrence,
                "type": r.type.value,
                "raw_type": r.raw_type,
                "side": r.side.value if r.side else None,
                "condition_id": r.condition_id,
                "token_id": r.token_id,
                "outcome": r.outcome,
                "outcome_index": r.outcome_index,
                "size": r.size,
                "usdc_size": r.usdc_size,
                "price": r.price,
                "tx_hash": r.tx_hash,
                "block_ts": r.block_ts,
                "detected_at": detected_at,
                "title": r.title,
                "slug": r.slug,
                "is_combo": r.is_combo,
                "is_backfill": r.block_ts < followed_since,
            }
            for r in rows
        ]
        inserted_ids: list[int] = []
        async with self._sm() as s, s.begin():
            # PostgreSQL caps bind parameters at 32767 per statement; chunk large backfills.
            for i in range(0, len(values), INSERT_CHUNK):
                inserted_ids += (
                    await s.scalars(
                        pg_insert(SourceEvent)
                        .values(values[i : i + INSERT_CHUNK])
                        .on_conflict_do_nothing(index_elements=["event_key"])
                        .returning(SourceEvent.id)
                    )
                ).all()
            if not inserted_ids:
                return []
            events: list[SourceEvent] = []
            for i in range(0, len(inserted_ids), INSERT_CHUNK):
                events += (
                    await s.scalars(
                        select(SourceEvent).where(
                            SourceEvent.id.in_(inserted_ids[i : i + INSERT_CHUNK])
                        )
                    )
                ).all()
            return sorted(events, key=lambda e: (e.block_ts, e.id))
