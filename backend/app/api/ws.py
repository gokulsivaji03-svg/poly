"""WebSocket fan-out with initial-state sync, heartbeats and per-event hydration.

Services publish lightweight internal events (ids/hints) on the EventBus. A single hub task
hydrates each event into its full API payload once, then fans it out to every client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.api import queries as q
from app.api.auth import decode_token
from app.runtime import Runtime

log = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_S = 15


class WsHub:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._extra: dict[str, Callable[[Any], Awaitable[Any]]] = {}

    def register_hydrator(self, topic: str, fn: Callable[[Any], Awaitable[Any]]) -> None:
        self._extra[topic] = fn

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ws-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def connect(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._clients.add(queue)
        return queue

    def disconnect(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._clients.discard(queue)

    async def _hydrate(self, topic: str, data: Any) -> Any | None:
        rt = self._rt
        if topic == "source_trade":
            rows = await q.source_trades(rt, limit=1, event_id=data["id"])
            return rows[0] if rows else None
        if topic == "copy_signal":
            rows = await q.copy_signals(rt, limit=1, signal_id=data["id"])
            return rows[0] if rows else None
        if topic == "portfolio":
            return await q.portfolio(rt)
        if topic == "wallet":
            w = await rt.registry.get(data["id"])
            if w is None:
                return None
            if w.removed_at is not None:
                return {"id": w.id, "removed": True}
            return await q.wallet_summary(rt, w)
        if topic in self._extra:
            return await self._extra[topic](data)
        if topic in ("settings", "system", "positions", "hedge"):
            return data
        return None  # internal-only topics

    async def _run(self) -> None:
        sub = self._rt.bus.subscribe()
        try:
            while True:
                msg = await sub.queue.get()
                topic = msg["type"]
                try:
                    payload = await self._hydrate(topic, msg["data"])
                except Exception:
                    log.exception("ws hydration failed", extra={"topic": topic})
                    continue
                if payload is None:
                    continue
                out = {"type": topic, "data": payload, "ts": self._rt.clock.now().isoformat()}
                for client in list(self._clients):
                    try:
                        client.put_nowait(out)
                    except asyncio.QueueFull:
                        # Slow client: drop it; it reconnects and resyncs from the snapshot.
                        self._clients.discard(client)
        finally:
            self._rt.bus.unsubscribe(sub)

    def is_connected(self, queue: asyncio.Queue[dict[str, Any]]) -> bool:
        return queue in self._clients


@router.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    rt: Runtime = websocket.app.state.runtime
    hub: WsHub = websocket.app.state.ws_hub
    token = websocket.query_params.get("token", "")
    if decode_token(rt.cfg, token) is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    queue = hub.connect()
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "data": {"dashboard": await q.dashboard(rt)},
                "ts": rt.clock.now().isoformat(),
            }
        )

        async def reader() -> None:
            while True:
                msg = await websocket.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await queue.put({"type": "pong", "data": {}})

        read_task = asyncio.create_task(reader())
        try:
            while True:
                if read_task.done():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    msg = {"type": "heartbeat", "data": {}}
                if not hub.is_connected(queue):
                    await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
                    break
                await websocket.send_json(msg)
        finally:
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await read_task
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(queue)
