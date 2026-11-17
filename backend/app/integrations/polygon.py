"""Polygon reads: optional source-wallet collateral, and exchange fill logs for fast detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.market import Side
from app.integrations.http import ResilientHttp, UpstreamError

log = logging.getLogger(__name__)

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
BALANCE_OF = "0x70a08231"
DECIMALS = Decimal(10) ** 6

# Polymarket exchange contracts (standard and neg-risk) and their per-order fill event,
# verified against Data API trades (see docs/polymarket-api-research.md). Topics:
# [event, orderHash, maker, taker]; data words: [side (0 BUY / 1 SELL), tokenId,
# makerAmountFilled, takerAmountFilled, fee, ...], amounts in 6-decimal units. Every order a
# wallet gets filled on, maker or taker, emits one event with that wallet as ``maker``.
EXCHANGES = (
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
)
FILL_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"


@dataclass(frozen=True, slots=True)
class ChainFill:
    tx_hash: str
    log_index: int
    block_number: int
    maker: str
    side: Side
    token_id: str
    shares: Decimal
    usdc: Decimal

    @property
    def price(self) -> Decimal:
        return self.usdc / self.shares


def decode_fill(entry: dict[str, Any]) -> ChainFill | None:
    """Decode one exchange fill log; ``None`` for anything that is not a clean BUY/SELL."""
    topics = entry.get("topics") or []
    data = str(entry.get("data") or "")[2:]
    if len(topics) < 4 or topics[0].lower() != FILL_TOPIC or len(data) < 64 * 4:
        return None
    words = [int(data[i : i + 64], 16) for i in range(0, 64 * 4, 64)]
    side = {0: Side.BUY, 1: Side.SELL}.get(words[0])
    if side is None:
        return None
    maker_amount, taker_amount = Decimal(words[2]) / DECIMALS, Decimal(words[3]) / DECIMALS
    if side is Side.BUY:
        usdc, shares = maker_amount, taker_amount
    else:
        usdc, shares = taker_amount, maker_amount
    if usdc <= 0 or shares <= 0:
        return None
    return ChainFill(
        tx_hash=str(entry["transactionHash"]).lower(),
        log_index=int(entry["logIndex"], 16),
        block_number=int(entry["blockNumber"], 16),
        maker="0x" + topics[2][-40:].lower(),
        side=side,
        token_id=str(words[1]),
        shares=shares,
        usdc=usdc,
    )


def _topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


class PolygonRpc:
    """Minimal JSON-RPC client for block heads, timestamps and exchange fill logs."""

    def __init__(self, http: ResilientHttp, url: str) -> None:
        self._http = http
        self._url = url
        self._block_ts: dict[int, int] = {}

    async def _call(self, method: str, params: list[Any]) -> Any:
        body = await self._http.post_json(
            self._url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if not isinstance(body, dict) or "result" not in body:
            raise UpstreamError(f"rpc {method} failed: {body!r:.200}")
        return body["result"]

    async def block_number(self) -> int:
        return int(await self._call("eth_blockNumber", []), 16)

    async def block_timestamp(self, number: int) -> int:
        ts = self._block_ts.get(number)
        if ts is None:
            block = await self._call("eth_getBlockByNumber", [hex(number), False])
            ts = int(block["timestamp"], 16)
            if len(self._block_ts) > 4096:
                self._block_ts.clear()
            self._block_ts[number] = ts
        return ts

    async def fills(self, from_block: int, to_block: int, makers: list[str]) -> list[ChainFill]:
        """Fills in ``[from_block, to_block]`` whose maker is one of ``makers``, in log order."""
        if not makers:
            return []
        logs = await self._call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "address": list(EXCHANGES),
                    "topics": [FILL_TOPIC, None, [_topic(m) for m in makers]],
                }
            ],
        )
        out = [f for f in (decode_fill(e) for e in logs) if f is not None]
        return sorted(out, key=lambda f: (f.block_number, f.log_index))


class CollateralReader:
    def __init__(self, http: ResilientHttp, rpc_url: str | None) -> None:
        self._http = http
        self._rpc = rpc_url

    @property
    def enabled(self) -> bool:
        return self._rpc is not None

    async def balance(self, wallet: str) -> Decimal | None:
        """Free collateral held by ``wallet``; ``None`` when unobservable (never 0 on error)."""
        if self._rpc is None:
            return None
        total = Decimal(0)
        for i, token in enumerate((PUSD, USDC_E)):
            data = BALANCE_OF + wallet.lower().removeprefix("0x").rjust(64, "0")
            payload = {
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_call",
                "params": [{"to": token, "data": data}, "latest"],
            }
            try:
                body = await self._http.post_json(self._rpc, payload)
            except UpstreamError as exc:
                log.info("collateral rpc failed", extra={"error": str(exc)})
                return None
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, str) or not result.startswith("0x"):
                return None
            total += Decimal(int(result, 16)) / DECIMALS
        return total
