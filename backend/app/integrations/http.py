"""Shared resilient HTTP client: pooling, retries with jittered backoff, Retry-After."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class UpstreamError(Exception):
    """Raised when an upstream call fails after retries. ``retryable`` hints at transience."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class HttpStats:
    requests: int = 0
    retries: int = 0
    errors: deque[float] = field(default_factory=lambda: deque(maxlen=1000))
    last_success_at: float | None = None

    def errors_since(self, seconds: float) -> int:
        cutoff = time.monotonic() - seconds
        return sum(1 for t in self.errors if t >= cutoff)


def backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 20.0) -> float:
    """Full-jitter exponential backoff (AWS style)."""
    return random.uniform(0, min(cap, base * (2**attempt)))  # noqa: S311 - not crypto


class ResilientHttp:
    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        max_retries: int = 5,
        client: httpx.AsyncClient | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"User-Agent": "polycopy/0.1 (+paper-trading research)"},
            http2=False,
        )
        self._max_retries = max_retries
        self._sleep = sleep
        self.stats = HttpStats()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self.stats.requests += 1
            try:
                resp = await self._client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                self.stats.errors.append(time.monotonic())
                delay = backoff_delay(attempt)
            else:
                if resp.status_code < 400:
                    self.stats.last_success_at = time.monotonic()
                    return resp.json()
                self.stats.errors.append(time.monotonic())
                if resp.status_code not in RETRYABLE_STATUS:
                    raise UpstreamError(
                        f"GET {url} -> {resp.status_code}: {resp.text[:300]}",
                        status=resp.status_code,
                        retryable=False,
                    )
                last_exc = UpstreamError(
                    f"GET {url} -> {resp.status_code}", status=resp.status_code
                )
                retry_after = resp.headers.get("Retry-After")
                delay = backoff_delay(attempt)
                if retry_after is not None:
                    with contextlib.suppress(ValueError):
                        delay = max(delay, float(retry_after))
            if attempt < self._max_retries:
                self.stats.retries += 1
                log.info(
                    "retrying upstream",
                    extra={"url": url, "attempt": attempt + 1, "delay_s": round(delay, 2)},
                )
                await self._sleep(delay)
        raise UpstreamError(f"GET {url} failed after retries: {last_exc}") from last_exc

    async def post_json(self, url: str, payload: Any) -> Any:
        self.stats.requests += 1
        resp = await self._client.post(url, json=payload)
        if resp.status_code >= 400:
            self.stats.errors.append(time.monotonic())
            raise UpstreamError(f"POST {url} -> {resp.status_code}", status=resp.status_code)
        self.stats.last_success_at = time.monotonic()
        return resp.json()
