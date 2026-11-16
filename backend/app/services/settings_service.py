"""Persisted, versioned strategy settings shared by API and workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AppSettings
from app.domain.settings import StrategySettings

Listener = Callable[[StrategySettings], Awaitable[None]]


class SettingsConflict(Exception):
    pass


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SettingsService:
    """Single settings document (row id=1) with optimistic concurrency via ``version``."""

    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm
        self._cache: StrategySettings | None = None
        self._version = 0
        self._lock = asyncio.Lock()
        self._listeners: list[Listener] = []

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    @property
    def version(self) -> int:
        return self._version

    async def get(self, *, refresh: bool = False) -> StrategySettings:
        if self._cache is not None and not refresh:
            return self._cache
        async with self._sm() as s, s.begin():
            row = await s.get(AppSettings, 1)
            if row is None:
                default = StrategySettings()
                await s.execute(
                    pg_insert(AppSettings)
                    .values(
                        id=1, version=1, document=default.model_dump(mode="json", by_alias=True)
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                row = await s.get(AppSettings, 1)
            assert row is not None
            self._cache = StrategySettings.model_validate(row.document)
            self._version = row.version
        return self._cache

    async def update(
        self, patch: dict[str, Any], *, expected_version: int | None = None
    ) -> StrategySettings:
        """Validate and persist a partial update; notify running strategies."""
        async with self._lock:
            current = await self.get(refresh=True)
            if expected_version is not None and expected_version != self._version:
                raise SettingsConflict(
                    f"settings changed (have v{self._version}, expected v{expected_version})"
                )
            merged = deep_merge(current.model_dump(mode="json", by_alias=True), patch)
            new = StrategySettings.model_validate(merged)
            async with self._sm() as s, s.begin():
                ok = await s.scalar(
                    update(AppSettings)
                    .where(AppSettings.id == 1, AppSettings.version == self._version)
                    .values(
                        document=new.model_dump(mode="json", by_alias=True),
                        version=AppSettings.version + 1,
                    )
                    .returning(AppSettings.version)
                )
                if ok is None:
                    raise SettingsConflict("settings were modified concurrently")
            self._cache = new
            self._version = ok
        for fn in self._listeners:
            await fn(new)
        return new

    async def current_version(self) -> int:
        async with self._sm() as s:
            v = await s.scalar(select(AppSettings.version).where(AppSettings.id == 1))
            return v or 0
