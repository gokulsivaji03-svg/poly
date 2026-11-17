"""TradeTape: idempotent, exactly-once persistence of source wallet activity."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SourceEvent
from app.domain.events import SourceActivity

INSERT_CHUNK = 1000


class TradeTape:
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm

    async def ingest(
        self,
        wallet_id: int,
        rows: Sequence[SourceActivity],
        *,
        detected_at: datetime,
        followed_since: datetime,
        source: str = "chain",
    ) -> list[SourceEvent]:
        """Insert unseen rows; return only the newly inserted ones, oldest first.

        Duplicates (backstop re-reads, stream + backstop overlap) collide on ``event_key``
        and are silently ignored, so a repeated event can never be mistaken for a new trade.

        The same trade can arrive from two sources (exchange logs and the Data API history
        import) with differently rounded fields. A TRADE is dropped when the *other* source
        already recorded the same (wallet, tx, token, side); a per-wallet transaction lock
        makes that check atomic with the insert, so a trade is recorded once.
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
                "source": source,
                "confirmation": "PENDING" if source == "mempool" else None,
            }
            for r in rows
        ]
        events: list[SourceEvent] = []
        async with self._sm() as s, s.begin():
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('source_events'), :w)"),
                {"w": wallet_id},
            )
            hashes = {v["tx_hash"] for v in values if v["type"] == "TRADE" and v["tx_hash"]}
            if hashes and source != "mempool":
                # A transaction is atomic: any confirmed row of it confirms its pending ones.
                await s.execute(
                    update(SourceEvent)
                    .where(
                        SourceEvent.wallet_id == wallet_id,
                        SourceEvent.source == "mempool",
                        SourceEvent.confirmation == "PENDING",
                        SourceEvent.tx_hash.in_(hashes | {h.lower() for h in hashes}),
                    )
                    .values(confirmation="CONFIRMED")
                )
            if hashes:
                taken = set(
                    (
                        await s.execute(
                            select(SourceEvent.tx_hash, SourceEvent.token_id, SourceEvent.side)
                            .where(
                                SourceEvent.wallet_id == wallet_id,
                                SourceEvent.type == "TRADE",
                                SourceEvent.source != source,
                                SourceEvent.tx_hash.in_(hashes | {h.lower() for h in hashes}),
                            )
                        )
                    ).all()
                )
                taken = {(h.lower(), t, sd) for h, t, sd in taken}
                values = [
                    v for v in values
                    if v["type"] != "TRADE"
                    or (v["tx_hash"].lower(), v["token_id"], v["side"]) not in taken
                ]
                if not values:
                    return []
            # PostgreSQL caps bind parameters at 32767 per statement; chunk large backfills.
            for i in range(0, len(values), INSERT_CHUNK):
                events += (
                    await s.scalars(
                        pg_insert(SourceEvent)
                        .values(values[i : i + INSERT_CHUNK])
                        .on_conflict_do_nothing(index_elements=["event_key"])
                        .returning(SourceEvent)
                    )
                ).all()
            return sorted(events, key=lambda e: (e.block_ts, e.id))
