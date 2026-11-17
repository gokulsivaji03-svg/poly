"""Polymarket Data API v2 client (public wallet activity, positions, value, freshness).

Raw httpx is used instead of the SDK paginator because incremental ingestion needs direct
control over the ``start`` window, sort direction and cursor walk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.money import opt_decimal
from app.domain.events import SourceActivity, assign_event_keys, parse_activity
from app.integrations.http import ResilientHttp

log = logging.getLogger(__name__)

PAGE_LIMIT = 500
MAX_PAGES = 40


@dataclass(frozen=True, slots=True)
class SourcePosition:
    token_id: str
    condition_id: str
    title: str
    outcome: str
    size: Decimal
    avg_price: Decimal | None
    current_price: Decimal | None
    current_value: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    opposite_token_id: str | None
    negative_risk: bool
    status: str


@dataclass(frozen=True, slots=True)
class WalletPnl:
    """Lifetime account P&L from ``/v2/user-pnl`` (includes redeemed/closed positions)."""

    realized: Decimal | None
    unrealized: Decimal | None


@dataclass(frozen=True, slots=True)
class ActivityBatch:
    rows: list[SourceActivity]
    malformed: int
    pages: int
    truncated: bool
    """True if MAX_PAGES was hit; the caller must not advance its high-water mark past rows."""


class DataApiClient:
    def __init__(self, http: ResilientHttp, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def activity_since(self, user: str, start: datetime) -> ActivityBatch:
        """All activity with block time >= ``start`` in ascending order, keyed and deduped."""
        params: dict[str, Any] = {
            "user": user,
            "start": max(1, int(start.timestamp())),
            "sort_direction": "ASC",
            "limit": PAGE_LIMIT,
        }
        rows: list[SourceActivity] = []
        malformed = 0
        pages = 0
        truncated = False
        cursor: str | None = None
        while True:
            q = {"user": user, "cursor": cursor} if cursor else params
            body = await self._http.get_json(f"{self._base}/v2/activity", q)
            pages += 1
            for raw in body.get("data") or []:
                try:
                    rows.append(parse_activity(raw))
                except (KeyError, ValueError, TypeError) as exc:
                    malformed += 1
                    log.warning("malformed activity row", extra={"error": str(exc)})
            pag = body.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                break
            if pages >= MAX_PAGES:
                truncated = True
                break
        return ActivityBatch(assign_event_keys(rows), malformed, pages, truncated)

    async def positions(self, user: str) -> list[SourcePosition]:
        out: list[SourcePosition] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            q: dict[str, Any] = {"user": user, "limit": PAGE_LIMIT}
            if cursor:
                q["cursor"] = cursor
            body = await self._http.get_json(f"{self._base}/v2/positions", q)
            for p in body.get("data") or []:
                out.append(
                    SourcePosition(
                        token_id=str(p.get("token_id") or ""),
                        condition_id=str(p.get("condition_id") or ""),
                        title=str(p.get("title") or ""),
                        outcome=str(p.get("outcome") or ""),
                        size=opt_decimal(p.get("current_size")) or Decimal(0),
                        avg_price=opt_decimal(p.get("avg_price")),
                        current_price=opt_decimal(p.get("current_price")),
                        current_value=opt_decimal(p.get("current_value")),
                        realized_pnl=opt_decimal(p.get("realized_pnl")),
                        unrealized_pnl=opt_decimal(p.get("unrealized_pnl")),
                        opposite_token_id=p.get("opposite_token_id") or None,
                        negative_risk=bool(p.get("negative_risk", False)),
                        status=str(p.get("status") or ""),
                    )
                )
            pag = body.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                break
        return out

    async def portfolio_value(self, user: str) -> Decimal | None:
        body = await self._http.get_json(f"{self._base}/v2/value", {"user": user})
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        return opt_decimal(data.get("value"))

    async def user_pnl(self, user: str) -> WalletPnl | None:
        """Latest lifetime P&L point; ``None`` if the wallet has no P&L history.

        ``/v2/positions`` cannot provide this: redeemed winners disappear from it while
        worthless losers linger as ``REDEEMABLE``, so summing it is biased toward losses.
        """
        body = await self._http.get_json(f"{self._base}/v2/user-pnl", {"user": user})
        points = (body.get("data") or {}).get("points") or []
        if not points:
            return None
        last = max(points, key=lambda p: p.get("timestamp") or 0)
        return WalletPnl(
            opt_decimal(last.get("realized_pnl")), opt_decimal(last.get("unrealized_pnl"))
        )

    async def pnl_history(self, user: str) -> list[tuple[datetime, Decimal]]:
        """Hourly lifetime P&L (trading, rebates, rewards and unrealized), oldest first."""
        body = await self._http.get_json(
            f"{self._base}/v2/user-pnl", {"user": user, "interval": "all"}
        )
        out: list[tuple[datetime, Decimal]] = []
        for p in (body.get("data") or {}).get("points") or []:
            total = opt_decimal(p.get("economic_pnl"))
            if total is None:
                parts = (opt_decimal(p.get("realized_pnl")), opt_decimal(p.get("unrealized_pnl")))
                total = None if parts[0] is None else parts[0] + (parts[1] or Decimal(0))
            if total is not None and p.get("timestamp"):
                out.append((datetime.fromtimestamp(int(p["timestamp"]), UTC), total))
        return sorted(out)

    async def status_lag_seconds(self) -> int | None:
        body = await self._http.get_json(f"{self._base}/v2/status")
        serving = (body.get("data") or {}).get("serving") or {}
        lag = serving.get("lag_seconds")
        return int(lag) if lag is not None else None

    async def token_refs(self, condition_id: str) -> list[dict[str, Any]]:
        body = await self._http.get_json(f"{self._base}/v2/tokens", {"condition": condition_id})
        return list(body.get("data") or [])
