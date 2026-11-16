"""PostgreSQL-backed fixtures. Requires a reachable test database (see README)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from app.core.clock import ManualClock
from app.db import models  # noqa: F401
from app.db.base import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from factories import NOW

TEST_DB = os.environ.get(
    "POLYCOPY_TEST_DATABASE_URL", "postgresql+asyncpg://localhost/polycopy_test"
)


async def _create_schema() -> None:
    engine = create_async_engine(TEST_DB, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    asyncio.run(_create_schema())


async def truncate_all(sm: async_sessionmaker[AsyncSession]) -> None:
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with sm() as s, s.begin():
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def sm() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DB, pool_size=20, max_overflow=0)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    await truncate_all(maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)
