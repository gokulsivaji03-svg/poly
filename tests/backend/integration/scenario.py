"""Deterministic mocked Polymarket world for end-to-end pipeline tests.

Data API, Gamma and Polygon RPC are mocked at the HTTP layer with respx; order books come
from an in-memory REST source; time is a ManualClock. Nothing touches the network.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import respx
from app.config import Settings
from app.core.clock import ManualClock
from app.domain.market import BookLevel, OrderBook
from app.integrations.http import ResilientHttp
from app.runtime import Runtime

DATA = "https://data-api.test"
GAMMA = "https://gamma.test"
RPC = "https://rpc.test"
COND = "0x" + "cd" * 32
YES = "101"
NO = "202"
WALLET_A = "0x" + "a1" * 20
WALLET_B = "0x" + "b2" * 20


async def _no_sleep(_: float) -> None:
    return None


class FakeRest:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.books: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
        self.fail = 0
        self.script: dict[str, list[tuple[list[tuple[str, str]], list[tuple[str, str]]]]] = {}
        """Per-token queue of books served on successive fetches (liquidity changes)."""

    def set(
        self, token: str, asks: list[tuple[str, str]], bids: list[tuple[str, str]] | None = None
    ) -> None:
        self.books[token] = (asks, bids or [])

    async def fetch(self, token_ids: list[str]) -> dict[str, OrderBook]:
        if self.fail:
            self.fail -= 1
            raise OSError("book endpoint down")
        out = {}
        for t in token_ids:
            if self.script.get(t):
                self.books[t] = self.script[t].pop(0)
            if t not in self.books:
                continue
            asks, bids = self.books[t]
            out[t] = OrderBook(
                token_id=t,
                condition_id=COND,
                asks=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
                bids=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids),
                received_at=self.clock.now(),
            )
        return out


def activity(
    wallet: str,
    *,
    ts: datetime,
    side: str | None = "BUY",
    size: str = "500",
    price: str = "0.40",
    token: str = YES,
    typ: str = "TRADE",
    tx: str = "0x01",
    usdc: str | None = None,
) -> dict[str, Any]:
    return {
        "proxy_wallet": wallet,
        "timestamp": int(ts.timestamp()),
        "condition_id": COND,
        "type": typ,
        "size": float(size),
        "usdc_size": float(usdc or Decimal(size) * Decimal(price)),
        "transaction_hash": tx,
        "price": float(price),
        "token_id": token,
        "side": side or "",
        "outcome_index": 0 if token == YES else 1,
        "outcome": "Yes" if token == YES else "No",
        "title": "Will it rain?",
        "slug": "rain",
    }


class World:
    """Mutable mocked upstream state. Routes read it on every request."""

    def __init__(self) -> None:
        self.activity: dict[str, list[dict[str, Any]]] = {}
        self.value: dict[str, str | None] = {}
        self.cash: dict[str, int | None] = {}
        self.positions: dict[str, list[dict[str, Any]]] = {}
        self.pnl: dict[str, tuple[str, str]] = {}
        self.activity_errors = 0
        self.fee_rate: str | None = "0"
        self.resolution: dict[str, str] | None = None

    def install(self, router: respx.MockRouter) -> None:
        router.get(f"{DATA}/v2/activity").mock(side_effect=self._activity)
        router.get(f"{DATA}/v2/value").mock(side_effect=self._value)
        router.get(f"{DATA}/v2/positions").mock(side_effect=self._positions)
        router.get(f"{DATA}/v2/user-pnl").mock(side_effect=self._user_pnl)
        router.get(f"{DATA}/v2/status").mock(
            return_value=httpx.Response(200, json={"data": {"serving": {"lag_seconds": 1}}})
        )
        router.get(f"{DATA}/v2/tokens").mock(side_effect=self._tokens)
        router.get(f"{GAMMA}/markets").mock(side_effect=self._gamma)
        router.post(RPC).mock(side_effect=self._rpc)
        router.get("https://geo.test/api/geoblock").mock(
            return_value=httpx.Response(
                200, json={"blocked": True, "country": "US", "region": "CA"}
            )
        )

    def _tokens(self, request: httpx.Request) -> httpx.Response:
        rows = []
        for token, other in ((YES, NO), (NO, YES)):
            row: dict[str, Any] = {
                "token_id": token,
                "condition_id": COND,
                "opposite_token_id": other,
                "resolved": self.resolution is not None,
            }
            if self.resolution is not None:
                row["final_price"] = float(self.resolution[token])
            rows.append(row)
        return httpx.Response(200, json={"data": rows})

    def _activity(self, request: httpx.Request) -> httpx.Response:
        if self.activity_errors:
            self.activity_errors -= 1
            return httpx.Response(503, json={"error": "busy"}, headers={"Retry-After": "0"})
        user = request.url.params["user"].lower()
        start = int(request.url.params.get("start", "1"))
        rows = sorted(
            (r for r in self.activity.get(user, []) if r["timestamp"] >= start),
            key=lambda r: r["timestamp"],
        )
        return httpx.Response(
            200,
            json={
                "data": rows,
                "pagination": {"has_more": False, "next_cursor": None, "limit": 500, "offset": 0},
            },
        )

    def _value(self, request: httpx.Request) -> httpx.Response:
        user = request.url.params["user"].lower()
        v = self.value.get(user)
        if v is None:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"data": {"proxy_wallet": user, "value": float(v)}})

    def _positions(self, request: httpx.Request) -> httpx.Response:
        user = request.url.params["user"].lower()
        return httpx.Response(
            200,
            json={
                "data": self.positions.get(user, []),
                "pagination": {"has_more": False, "next_cursor": None},
            },
        )

    def _user_pnl(self, request: httpx.Request) -> httpx.Response:
        user = request.url.params["user"].lower()
        points = []
        if user in self.pnl:
            realized, unrealized = self.pnl[user]
            points = [
                {"timestamp": 1, "realized_pnl": -1.0, "unrealized_pnl": 0.0},
                {
                    "timestamp": 2,
                    "realized_pnl": float(realized),
                    "unrealized_pnl": float(unrealized),
                },
            ]
        return httpx.Response(200, json={"data": {"proxy_wallet": user, "points": points}})

    def _gamma(self, request: httpx.Request) -> httpx.Response:
        m: dict[str, Any] = {
            "conditionId": COND,
            "question": "Will it rain?",
            "slug": "rain",
            "clobTokenIds": json.dumps([YES, NO]),
            "outcomes": json.dumps(["Yes", "No"]),
            "negRisk": False,
            "orderPriceMinTickSize": 0.01,
            "orderMinSize": 1,
            "active": True,
            "closed": False,
            "acceptingOrders": True,
        }
        if self.fee_rate is None:
            m["feesEnabled"] = True
        elif self.fee_rate == "0":
            m["feesEnabled"] = False
        else:
            m["feesEnabled"] = True
            m["feeSchedule"] = {"rate": float(self.fee_rate), "exponent": 1, "takerOnly": True}
        return httpx.Response(200, json=[m])

    def _rpc(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wallet = "0x" + body["params"][0]["data"][-40:]
        cash = self.cash.get(wallet.lower())
        if cash is None:
            return httpx.Response(500)
        # Split evenly across pUSD / USDC.e calls (id 0 and 1).
        units = cash * 10**6 // 2 if body["id"] == 0 else cash * 10**6 - cash * 10**6 // 2
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(units)})


def scenario_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        run_workers=False,
        data_api_url=DATA,
        gamma_api_url=GAMMA,
        polygon_rpc_url=RPC,
        geoblock_url="https://geo.test/api/geoblock",
        wallet_poll_interval_s=1,
        equity_snapshot_interval_s=60,
        ingest_overlap_s=120,
        _env_file=None,  # type: ignore[call-arg]
    )


async def make_runtime(db_url: str, clock: ManualClock, rest: FakeRest, engine: Any) -> Runtime:
    http = ResilientHttp(max_retries=3, sleep=_no_sleep)
    return await Runtime.create(
        scenario_settings(db_url),
        clock=clock,
        engine=engine,
        http=http,
        rest_books=rest,
        enable_stream=False,
    )
