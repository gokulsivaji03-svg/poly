"""Optional on-chain collateral reader for source wallets (pUSD + USDC.e on Polygon)."""

from __future__ import annotations

import logging
from decimal import Decimal

from app.integrations.http import ResilientHttp, UpstreamError

log = logging.getLogger(__name__)

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
BALANCE_OF = "0x70a08231"
DECIMALS = Decimal(10) ** 6


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
