"""Watched-wallet registry: which public wallets we observe and copy."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import WatchedWallet
from app.services.event_bus import EventBus

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletError(Exception):
    pass


class DuplicateWallet(WalletError):
    pass


def normalize_address(address: str) -> str:
    address = address.strip()
    if not ADDRESS_RE.match(address):
        raise WalletError("address must be 0x followed by 40 hex characters")
    return address.lower()


class WalletRegistry:
    def __init__(self, sm: async_sessionmaker[AsyncSession], clock: Clock, bus: EventBus) -> None:
        self._sm = sm
        self._clock = clock
        self._bus = bus

    async def add(self, address: str, nickname: str = "", follow: bool = True) -> WatchedWallet:
        addr = normalize_address(address)
        now = self._clock.now()
        async with self._sm() as s, s.begin():
            existing = await s.scalar(select(WatchedWallet).where(WatchedWallet.address == addr))
            if existing is not None and existing.removed_at is None:
                raise DuplicateWallet(addr)
            if existing is not None:
                existing.removed_at = None
                existing.follow = follow
                existing.nickname = nickname or existing.nickname
                existing.followed_since = now
                wallet = existing
            else:
                wallet = WatchedWallet(
                    address=addr, nickname=nickname, follow=follow, added_at=now, followed_since=now
                )
                s.add(wallet)
            await s.flush()
        self._bus.publish("wallet", {"id": wallet.id})
        return wallet

    async def update(self, wallet_id: int, fields: dict[str, Any]) -> WatchedWallet:
        allowed = {"nickname", "follow", "sizing_override"}
        async with self._sm() as s, s.begin():
            wallet = await s.get(WatchedWallet, wallet_id, with_for_update=True)
            if wallet is None or wallet.removed_at is not None:
                raise WalletError("wallet not found")
            for k, v in fields.items():
                if k in allowed:
                    setattr(wallet, k, v)
            if fields.get("follow") is True:
                # Re-enabling copying never back-copies trades made while copying was off.
                wallet.followed_since = max(wallet.followed_since, self._clock.now())
        self._bus.publish("wallet", {"id": wallet_id})
        return wallet

    async def remove(self, wallet_id: int) -> None:
        async with self._sm() as s, s.begin():
            res = await s.execute(
                update(WatchedWallet)
                .where(WatchedWallet.id == wallet_id, WatchedWallet.removed_at.is_(None))
                .values(removed_at=self._clock.now(), follow=False)
            )
            if res.rowcount == 0:  # type: ignore[attr-defined]
                raise WalletError("wallet not found")
        self._bus.publish("wallet", {"id": wallet_id})

    async def active(self) -> list[WatchedWallet]:
        async with self._sm() as s:
            return list(
                (
                    await s.scalars(
                        select(WatchedWallet)
                        .where(WatchedWallet.removed_at.is_(None))
                        .order_by(WatchedWallet.id)
                    )
                ).all()
            )

    async def get(self, wallet_id: int) -> WatchedWallet | None:
        async with self._sm() as s:
            return await s.get(WatchedWallet, wallet_id)
