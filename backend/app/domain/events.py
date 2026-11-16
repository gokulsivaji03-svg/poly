"""Normalized public wallet activity and its deterministic identity."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.money import ZERO, to_decimal
from app.domain.market import Side


class ActivityType(StrEnum):
    TRADE = "TRADE"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    REDEEM = "REDEEM"
    CONVERSION = "CONVERSION"
    REWARD = "REWARD"
    OTHER = "OTHER"

    @classmethod
    def parse(cls, raw: str) -> ActivityType:
        try:
            return cls(raw.upper())
        except ValueError:
            return cls.OTHER


class SourceActivity(BaseModel):
    """One public activity row. Only ``TRADE`` rows with a side are copy candidates."""

    model_config = ConfigDict(frozen=True)

    wallet: str
    type: ActivityType
    raw_type: str
    side: Side | None
    condition_id: str
    token_id: str
    outcome: str
    outcome_index: int | None
    size: Decimal
    usdc_size: Decimal
    price: Decimal
    tx_hash: str
    block_ts: datetime
    title: str = ""
    slug: str = ""
    is_combo: bool = False
    event_key: str = ""
    occurrence: int = 0

    @property
    def is_copyable_trade(self) -> bool:
        return (
            self.type is ActivityType.TRADE
            and self.side is not None
            and not self.is_combo
            and bool(self.token_id)
            and bool(self.condition_id)
            and self.size > ZERO
            and ZERO < self.price < Decimal(1)
        )


def _canonical(a: SourceActivity) -> str:
    parts = (
        a.wallet.lower(),
        a.tx_hash.lower(),
        a.raw_type.upper(),
        a.token_id,
        a.side.value if a.side else "",
        f"{a.size.normalize():f}",
        f"{a.usdc_size.normalize():f}",
        f"{a.price.normalize():f}",
        str(int(a.block_ts.timestamp())),
    )
    return "|".join(parts)


def assign_event_keys(rows: Iterable[SourceActivity]) -> list[SourceActivity]:
    """Give each row a stable key; identical rows in one tx get increasing occurrence ordinals.

    The Data API exposes no event id, and one transaction can carry several byte-identical
    fills for the same wallet and token. Rows are ordered deterministically before ordinals
    are assigned, so re-reading an overlapping window reproduces the same keys.
    """
    ordered = sorted(rows, key=lambda r: (r.block_ts, r.tx_hash, _canonical(r)))
    seen: Counter[str] = Counter()
    out: list[SourceActivity] = []
    for r in ordered:
        canon = _canonical(r)
        n = seen[canon]
        seen[canon] += 1
        key = hashlib.sha256(f"{canon}|{n}".encode()).hexdigest()
        out.append(r.model_copy(update={"event_key": key, "occurrence": n}))
    return out


def parse_activity(row: dict[str, Any]) -> SourceActivity:
    """Map a Data API v2 ``/v2/activity`` row to a domain object. Raises on malformed rows."""
    raw_type = str(row.get("type") or "")
    side_raw = str(row.get("side") or "").upper()
    outcome_index = row.get("outcome_index")
    return SourceActivity(
        wallet=str(row["proxy_wallet"]).lower(),
        type=ActivityType.parse(raw_type),
        raw_type=raw_type,
        side=Side(side_raw) if side_raw in ("BUY", "SELL") else None,
        condition_id=str(row.get("condition_id") or ""),
        token_id=str(row.get("token_id") or ""),
        outcome=str(row.get("outcome") or ""),
        outcome_index=None if outcome_index in (None, 999) else int(outcome_index),
        size=to_decimal(row.get("size") or 0),
        usdc_size=to_decimal(row.get("usdc_size") or 0),
        price=to_decimal(row.get("price") or 0),
        tx_hash=str(row.get("transaction_hash") or ""),
        block_ts=datetime.fromtimestamp(int(row["timestamp"]), tz=UTC),
        title=str(row.get("title") or ""),
        slug=str(row.get("slug") or ""),
        is_combo=bool(row.get("is_combo", False)),
    )
