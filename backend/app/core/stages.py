"""Per-stage latency of one copy, from the moment its fill reached this process.

Marks use the monotonic ``perf_counter_ns`` clock, so they are immune to wall-clock steps
and comparable to each other within one process. They are stored as microseconds since
``RECEIVED``; wall-clock lag before receipt (block time to detection) is ``detection_lag_ms``.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Stage(StrEnum):
    RECEIVED = "received"
    """Fill log in hand: a WebSocket frame, or an ``eth_getLogs`` response."""
    TAPED = "taped"
    """Source event committed to the trade tape."""
    STARTED = "started"
    """Copy began: signal journaled, settings read, the wallet's earlier copies finished and,
    for a BUY, the market lock held. In a burst this span is mostly queueing."""
    CONTEXT = "context"
    """Market, book, equity and portfolio loaded."""
    DECIDED = "decided"
    """Strategy and pre-trade risk produced an approved or rejected order."""
    RESERVED = "reserved"
    """Cash reserved for a BUY."""
    EXECUTED = "executed"
    """Executor returned: fill booked, or order rejected."""


@dataclass(slots=True)
class StageTimer:
    origin_ns: int = field(default_factory=time.perf_counter_ns)
    marks: dict[Stage, int] = field(default_factory=dict)

    def mark(self, stage: Stage) -> None:
        self.marks[stage] = time.perf_counter_ns()

    def fork(self) -> StageTimer:
        """An independent copy, for each event of a batch received together."""
        return StageTimer(self.origin_ns, dict(self.marks))

    def as_us(self) -> dict[str, int]:
        return {s.value: (ns - self.origin_ns) // 1000 for s, ns in self.marks.items()}


@dataclass(frozen=True, slots=True)
class Percentiles:
    n: int
    p50: float
    p90: float
    p99: float
    max: float


def percentiles(values: list[float]) -> Percentiles:
    values = sorted(values)
    if len(values) == 1:
        v = values[0]
        return Percentiles(1, v, v, v, v)
    q = statistics.quantiles(values, n=100, method="inclusive")
    return Percentiles(len(values), q[49], q[89], q[98], values[-1])


def summarize(rows: Iterable[Mapping[str, int]]) -> dict[str, Percentiles]:
    """Percentiles (µs) of each stage's own duration and of the whole path.

    A stage's duration is its mark minus the latest earlier mark present in the same row,
    so rows that stop early (a rejected signal never reserves) still count where they ran.
    """
    order = [s.value for s in Stage]
    spans: dict[str, list[float]] = {}
    for row in rows:
        previous = 0
        for name in order[1:]:
            at = row.get(name)
            if at is None:
                continue
            spans.setdefault(name, []).append(at - previous)
            previous = at
        if previous:
            spans.setdefault("total", []).append(previous)
    return {name: percentiles(v) for name, v in spans.items()}


def render(summary: Mapping[str, Percentiles]) -> str:
    lines = [f"{'stage':<10}{'n':>7}{'p50 ms':>10}{'p90 ms':>10}{'p99 ms':>10}{'max ms':>10}"]
    for name, p in summary.items():
        cells = (p.p50, p.p90, p.p99, p.max)
        lines.append(f"{name:<10}{p.n:>7}" + "".join(f"{c / 1000:>10.2f}" for c in cells))
    return "\n".join(lines)
