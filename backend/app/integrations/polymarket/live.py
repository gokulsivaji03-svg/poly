"""Live exchange adapter — disabled by default and gated on every order.

Live trading on Polymarket International requires *all* of:

1. ``POLYCOPY_EXECUTION_MODE=LIVE`` and ``POLYCOPY_LIVE_TRADING_ENABLED=true``;
2. a private key and funder (proxy) address supplied through the environment;
3. a geoblock check that affirmatively reports the caller as **not** blocked, performed at
   startup and again immediately before every order (unknown counts as blocked).

The adapter never attempts to bypass geographic restrictions. Polymarket US is a separate
venue (KYC'd API keys, different market structure) and is reported as unsupported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol

from app.config import ExecutionMode, Settings
from app.core.money import ZERO, to_decimal
from app.domain.fills import FillLeg, FillSimulation
from app.domain.market import Side
from app.integrations.polymarket.geoblock import GeoblockChecker, GeoStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VenueStatus:
    id: str
    name: str
    live_supported: bool
    live_enabled: bool
    reason: str | None
    geoblock: GeoStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        geo = None
        if self.geoblock is not None:
            geo = {
                "blocked": self.geoblock.blocked,
                "country": self.geoblock.country,
                "region": self.geoblock.region,
                "checked_at": self.geoblock.checked_at.isoformat(),
            }
        return {
            "id": self.id,
            "name": self.name,
            "live_supported": self.live_supported,
            "live_enabled": self.live_enabled,
            "reason": self.reason,
            "geoblock": geo,
        }


US_VENUE = VenueStatus(
    id="polymarket_us",
    name="Polymarket US (CFTC-regulated DCM)",
    live_supported=False,
    live_enabled=False,
    reason=(
        "Separate venue: KYC'd API keys at api.polymarket.us, single-instrument "
        "long/short market structure and non-public accounts. Not integrated; public "
        "International wallets cannot be mirrored one-to-one there."
    ),
)


class LiveGate:
    def __init__(self, cfg: Settings, geoblock: GeoblockChecker) -> None:
        self._cfg = cfg
        self._geo = geoblock

    def static_blockers(self) -> list[str]:
        cfg = self._cfg
        out: list[str] = []
        if cfg.execution_mode is not ExecutionMode.LIVE:
            out.append(f"Execution mode is {cfg.execution_mode.value}")
        if not cfg.live_trading_enabled:
            out.append("POLYCOPY_LIVE_TRADING_ENABLED is false")
        if cfg.live_private_key is None or not cfg.live_funder_address:
            out.append("Live credentials not configured")
        return out

    async def check(self, *, max_age: timedelta | None = None) -> VenueStatus:
        """Eligibility now. ``max_age`` reuses a geoblock result up to that old instead of
        making an HTTPS call; a reused blocked or failed result still refuses."""
        blockers = self.static_blockers()
        geo = await self._geo.check() if max_age is None else await self._geo.current(max_age)
        if geo.blocked is None:
            blockers.append(f"Geoblock check failed ({geo.error}); treated as restricted")
        elif geo.blocked:
            blockers.append(f"Geoblocked jurisdiction: {geo.country}/{geo.region}")
        return VenueStatus(
            id="polymarket_international",
            name="Polymarket International (CLOB on Polygon)",
            live_supported=True,
            live_enabled=not blockers,
            reason="; ".join(blockers) or None,
            geoblock=geo,
        )


class LiveOrderClient(Protocol):
    async def place_market_order(self, **kwargs: Any) -> Any: ...


class LiveTradingUnavailable(RuntimeError):
    pass


def fill_from_exchange(side: Side, response: Any) -> FillSimulation | None:
    """Translate an accepted FAK order into a booked fill using exchange-reported amounts.

    BUY: ``making_amount`` = USDC paid, ``taking_amount`` = shares received.
    SELL: ``making_amount`` = shares sold, ``taking_amount`` = USDC received.
    Fees are embedded in the exchange amounts; any residual is reconciled from trade history.
    """
    making = to_decimal(getattr(response, "making_amount", None) or 0)
    taking = to_decimal(getattr(response, "taking_amount", None) or 0)
    shares, usdc = (taking, making) if side is Side.BUY else (making, taking)
    if shares <= ZERO or usdc <= ZERO:
        return None
    avg = usdc / shares
    return FillSimulation(
        side=side,
        legs=(FillLeg(avg, shares),),
        gross_shares=shares,
        net_shares=shares,
        notional=usdc,
        fee_usdc=ZERO,
        cash_delta=-usdc if side is Side.BUY else usdc,
        limit_price=None,
        exhausted_book=False,
    )


async def place_live_order(
    client: LiveOrderClient,
    *,
    token_id: str,
    side: Side,
    budget: Decimal | None,
    shares: Decimal | None,
    limit_price: Decimal | None,
) -> Any:
    kwargs: dict[str, Any] = {"token_id": token_id, "side": side.value, "order_type": "FAK"}
    if side is Side.BUY:
        if budget is not None and shares is None:
            kwargs["amount"] = budget
        else:
            kwargs["shares"] = shares
            if budget is not None:
                kwargs["max_spend"] = budget
        if limit_price is not None:
            kwargs["max_price"] = limit_price
    else:
        kwargs["shares"] = shares
        if limit_price is not None:
            kwargs["min_price"] = limit_price
    return await client.place_market_order(**kwargs)
