"""Geographic eligibility check. Never bypassed: a blocked or unknown result disables live."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.clock import Clock
from app.integrations.http import ResilientHttp, UpstreamError


@dataclass(frozen=True, slots=True)
class GeoStatus:
    blocked: bool | None
    """``None`` when the check itself failed; treated as ineligible."""
    country: str | None
    region: str | None
    checked_at: datetime
    error: str | None = None

    @property
    def eligible(self) -> bool:
        return self.blocked is False


class GeoblockChecker:
    def __init__(self, http: ResilientHttp, url: str, clock: Clock) -> None:
        self._http = http
        self._url = url
        self._clock = clock
        self.last: GeoStatus | None = None

    async def check(self) -> GeoStatus:
        try:
            body = await self._http.get_json(self._url)
            status = GeoStatus(
                blocked=bool(body.get("blocked")) if "blocked" in body else None,
                country=body.get("country"),
                region=body.get("region"),
                checked_at=self._clock.now(),
            )
        except UpstreamError as exc:
            status = GeoStatus(None, None, None, self._clock.now(), error=str(exc))
        self.last = status
        return status

    async def current(self, max_age: timedelta) -> GeoStatus:
        """The last result if it is at most ``max_age`` old, else a fresh check."""
        last = self.last
        if last is not None and self._clock.now() - last.checked_at <= max_age:
            return last
        return await self.check()
