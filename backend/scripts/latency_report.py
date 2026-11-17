"""Per-stage copy latency from a polycopy database.

    uv run python scripts/latency_report.py [--hours 24] [--source chain]

Stage durations are monotonic µs between consecutive marks in ``copy_signals.stage_us``
(see ``app.core.stages``). ``detection`` is wall clock from block time to receipt.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.core.stages import percentiles, render, summarize
from app.db.models import CopySignal, SourceEvent
from app.db.session import make_engine
from sqlalchemy import select


async def report(hours: float, source: str | None) -> str:
    engine = make_engine(get_settings().database_url, pool_size=1)
    since = datetime.now(UTC) - timedelta(hours=hours)
    query = (
        select(CopySignal.stage_us, CopySignal.detection_lag_ms)
        .join(SourceEvent, SourceEvent.id == CopySignal.source_event_id)
        .where(CopySignal.stage_us.is_not(None), CopySignal.detected_at >= since)
    )
    if source is not None:
        query = query.where(SourceEvent.source == source)
    async with engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    await engine.dispose()
    if not rows:
        return f"No timed copy signals in the last {hours:g} h."
    summary = summarize(stage for stage, _ in rows)
    lags = [lag * 1000 for _, lag in rows if lag is not None]
    if lags:
        summary = {"detection": percentiles(lags), **summary}
    return render(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--source", choices=["chain", "mempool", "api"])
    args = parser.parse_args()
    print(asyncio.run(report(args.hours, args.source)))


if __name__ == "__main__":
    main()
