"""In-process pub/sub used to fan out domain events to WebSocket clients.

Slow subscribers never block publishers: each subscriber has a bounded queue and is marked
as lagging (and dropped by the transport) when it overflows, forcing a client resync.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=500))
    overflowed: bool = False


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def publish(self, topic: str, data: Any) -> None:
        msg = {"type": topic, "data": data}
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                sub.overflowed = True
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                log.warning("websocket subscriber overflowed; client will resync")

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
