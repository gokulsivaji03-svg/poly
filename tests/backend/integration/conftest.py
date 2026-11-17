"""PostgreSQL-backed fixtures. Requires a reachable test database (see README)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from app.core.clock import ManualClock
from app.db import models  # noqa: F401
from app.db.base import Base
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from factories import NOW

_BASE_URL = make_url(
    os.environ.get("POLYCOPY_TEST_DATABASE_URL", "postgresql+asyncpg://localhost/polycopy_test")
)
# A database of our own per run: tables other projects add to the shared test database
# cannot break drop_all, and concurrent runs cannot truncate each other's rows.
TEST_DB = _BASE_URL.set(
    database=f"{_BASE_URL.database}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
).render_as_string(hide_password=False)


async def _admin(statement: str) -> None:
    engine = create_async_engine(
        _BASE_URL.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as conn:
        await conn.execute(text(statement))
    await engine.dispose()


async def _create_database() -> None:
    name = make_url(TEST_DB).database
    await _admin(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(TEST_DB, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database() -> Iterator[None]:
    asyncio.run(_create_database())
    yield
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{make_url(TEST_DB).database}" WITH (FORCE)'))


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
