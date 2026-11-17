"""Measure how early Polymarket fills are visible in the Polygon mempool.

    uv run python scripts/mempool_probe.py [--minutes 10] [--ws wss://...]

Two subscriptions run side by side on the same endpoint:

- ``newPendingTransactions`` with full bodies: operator ``matchOrders`` calls to the
  exchanges, decoded into one expected fill per order (maker, token, side, maker amount);
- ``logs``: the exchanges' ``OrderFilled`` events once the block lands.

Each confirmed fill is joined to its pending sighting on (tx, maker, token, side). The
report gives coverage (share of confirmed fills seen pending first), lead time (log
arrival minus pending arrival) and decode agreement (pending maker amount equals the
log's ``makerAmountFilled``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import websockets
from app.integrations.polygon import (
    EXCHANGES,
    FILL_TOPIC,
    ChainFill,
    decode_fill,
    decode_pending_fills,
)

Key = tuple[str, str, str, str]
"""(tx_hash, maker, token_id, side)"""


def _key(f: ChainFill) -> tuple[Key, Decimal]:
    given = f.usdc if f.side.value == "BUY" else f.shares  # the leg a pending decode knows
    return (f.tx_hash, f.maker, f.token_id, f.side.value), given


@dataclass
class Probe:
    pending: dict[Key, tuple[float, Decimal]] = field(default_factory=dict)
    confirmed: dict[Key, tuple[float, Decimal]] = field(default_factory=dict)

    def on_pending(self, tx: dict[str, Any], now: float) -> None:
        for f in decode_pending_fills(tx):
            key, given = _key(f)
            self.pending.setdefault(key, (now, given))

    def on_log(self, log: dict[str, Any], now: float) -> None:
        f = decode_fill(log)
        if f is not None:
            key, given = _key(f)
            self.confirmed.setdefault(key, (now, given))

    def report(self) -> str:
        leads, agree = [], 0
        for key, (t_log, given) in self.confirmed.items():
            seen = self.pending.get(key)
            if seen is None:
                continue
            leads.append((t_log - seen[0]) * 1000)
            agree += seen[1] == given
        n = len(self.confirmed)
        if not leads:
            return f"{n} confirmed fills, none seen pending first"
        q = statistics.quantiles(leads, n=10) if len(leads) > 1 else leads * 9
        return "\n".join([
            f"confirmed fills          {n}",
            f"seen pending first       {len(leads)} ({len(leads) / n:.0%})",
            f"lead ms p10/p50/p90      {q[0]:.0f} / {statistics.median(leads):.0f} / {q[8]:.0f}",
            f"decode agreement         {agree}/{len(leads)} maker-asset amounts equal",
        ])


async def _stream(url: str, params: list[Any], handle) -> None:
    """Subscribe and feed ``handle``; public nodes drop sockets, so reconnect forever."""
    while True:
        try:
            async with websockets.connect(
                url, max_size=None, open_timeout=10, ping_interval=20
            ) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                          "params": params}))
                await ws.recv()
                async for raw in ws:
                    result = json.loads(raw).get("params", {}).get("result")
                    if isinstance(result, dict):
                        handle(result, time.monotonic())
        except (OSError, websockets.ConnectionClosed, TimeoutError):
            await asyncio.sleep(1)


async def run(url: str, minutes: float) -> str:
    probe = Probe()
    log_filter = {"address": list(EXCHANGES), "topics": [FILL_TOPIC]}
    tasks = [
        asyncio.create_task(_stream(url, ["newPendingTransactions", True], probe.on_pending)),
        asyncio.create_task(_stream(url, ["logs", log_filter], probe.on_log)),
    ]
    done, _ = await asyncio.wait(tasks, timeout=minutes * 60, return_when="FIRST_EXCEPTION")
    for t in tasks:
        t.cancel()
    for t in done:
        if t.exception():
            raise t.exception()  # type: ignore[misc]
    return probe.report()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--ws", default="wss://polygon-bor-rpc.publicnode.com")
    args = ap.parse_args()
    print(asyncio.run(run(args.ws, args.minutes)))


if __name__ == "__main__":
    main()
