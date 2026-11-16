"""Market metadata and outcome-token mapping (Gamma + Data API ``/v2/tokens``).

Gamma's ``clobTokenIds``/``outcomes`` define the tradable tokens; ``/v2/tokens``
``opposite_token_id`` is used to cross-validate the complement. Any disagreement marks the
market as non-standard so the hedge scanner refuses to pair it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.clock import Clock
from app.core.money import ZERO, opt_decimal, to_decimal
from app.domain.market import FeeSchedule, MarketInfo, OutcomeToken
from app.integrations.http import ResilientHttp, UpstreamError
from app.integrations.polymarket.data_api import DataApiClient

log = logging.getLogger(__name__)

# Highest documented taker rate; used for copy sizing when a market's schedule is unknown.
CONSERVATIVE_FEE = FeeSchedule(enabled=True, rate=Decimal("0.07"))


def _json_list(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_gamma_market(m: dict[str, Any], fetched_at: datetime) -> MarketInfo:
    token_ids = [str(t) for t in _json_list(m.get("clobTokenIds"))]
    outcomes = [str(o) for o in _json_list(m.get("outcomes"))]
    tokens = tuple(
        OutcomeToken(
            token_id=t, outcome=outcomes[i] if i < len(outcomes) else f"#{i}", outcome_index=i
        )
        for i, t in enumerate(token_ids)
    )
    fs = m.get("feeSchedule")
    fees_known = True
    if m.get("feesEnabled") is False:
        schedule = FeeSchedule(enabled=False)
    elif isinstance(fs, dict) and fs.get("rate") is not None:
        schedule = FeeSchedule(
            enabled=True,
            rate=to_decimal(fs["rate"]),
            exponent=int(fs.get("exponent") or 1),
            taker_only=bool(fs.get("takerOnly", True)),
        )
    else:
        schedule = CONSERVATIVE_FEE
        fees_known = False
    return MarketInfo(
        condition_id=str(m.get("conditionId") or ""),
        question=str(m.get("question") or ""),
        slug=str(m.get("slug") or ""),
        tokens=tokens,
        neg_risk=bool(m.get("negRisk", False)),
        fee_schedule=schedule,
        fees_known=fees_known,
        tick_size=opt_decimal(m.get("orderPriceMinTickSize")) or Decimal("0.01"),
        min_order_size=opt_decimal(m.get("orderMinSize")) or ZERO,
        active=bool(m.get("active", True)),
        closed=bool(m.get("closed", False)),
        accepting_orders=bool(m.get("acceptingOrders", True)),
        end_date=_parse_dt(m.get("endDate")),
        fetched_at=fetched_at,
    )


class MarketMetadataClient:
    def __init__(
        self, http: ResilientHttp, gamma_url: str, data_api: DataApiClient, clock: Clock
    ) -> None:
        self._http = http
        self._gamma = gamma_url.rstrip("/")
        self._data = data_api
        self._clock = clock

    async def fetch(self, condition_id: str) -> MarketInfo | None:
        body = await self._http.get_json(
            f"{self._gamma}/markets", {"condition_ids": condition_id, "limit": 5}
        )
        rows = body if isinstance(body, list) else body.get("data", [])
        match = next(
            (m for m in rows if str(m.get("conditionId", "")).lower() == condition_id.lower()), None
        )
        if match is None:
            return None
        info = parse_gamma_market(match, self._clock.now())
        return await self._validate_complement(info)

    async def _validate_complement(self, info: MarketInfo) -> MarketInfo:
        if not info.is_standard_binary:
            return info
        try:
            refs = await self._data.token_refs(info.condition_id)
        except UpstreamError as exc:
            log.warning(
                "token refs unavailable", extra={"condition": info.condition_id, "error": str(exc)}
            )
            return info.model_copy(update={"complement_verified": False})
        by_id = {str(r.get("token_id")): r for r in refs}
        a, b = info.tokens
        ra, rb = by_id.get(a.token_id), by_id.get(b.token_id)
        resolved = bool(ra and ra.get("resolved")) or bool(rb and rb.get("resolved"))
        ok = (
            ra is not None
            and rb is not None
            and str(ra.get("opposite_token_id")) == b.token_id
            and str(rb.get("opposite_token_id")) == a.token_id
            and ra.get("condition_id", "").lower() == info.condition_id.lower()
        )
        payouts = {}
        if resolved:
            for t, r in ((a, ra), (b, rb)):
                fp = r.get("final_price") if r else None
                if fp is not None:
                    payouts[t.token_id] = to_decimal(fp)
        return info.model_copy(
            update={
                "complement_verified": ok,
                "resolved": resolved,
                "payouts": payouts,
            }
        )
