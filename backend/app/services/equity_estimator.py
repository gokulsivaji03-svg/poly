"""WalletEquityEstimator: observable bankroll of source wallets, point-in-time.

Observation = marked positions (``/v2/value``) + optional on-chain collateral. Without
collateral the estimate is a *lower bound* and is labelled so; a failed observation is
``UNAVAILABLE`` and never reported as zero.

``estimate_at(ts)`` picks the snapshot nearest to the trade and rolls it to the instant just
before the trade using the wallet's own recorded activity in between, so sizing uses the
source's pre-trade equity rather than its current equity.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.money import ZERO, hours_between
from app.db.models import SourceEvent, WalletEquitySnapshot, WalletPosition, WatchedWallet
from app.domain.equity import EquityCoverage, EquityEstimate, unavailable
from app.integrations.http import UpstreamError
from app.integrations.polygon import CollateralReader
from app.integrations.polymarket.data_api import DataApiClient

log = logging.getLogger(__name__)

BASE_CONFIDENCE = {
    EquityCoverage.POSITIONS_AND_CASH: Decimal("0.95"),
    EquityCoverage.POSITIONS_ONLY: Decimal("0.60"),
    EquityCoverage.UNAVAILABLE: ZERO,
}
HALF_LIFE_HOURS = Decimal(2)

# Approximate effect of each activity type on marked positions value (cash moves opposite).
_POSITION_SIGN = {
    ("TRADE", "BUY"): 1,
    ("TRADE", "SELL"): -1,
    ("SPLIT", None): 1,
    ("MERGE", None): -1,
    ("REDEEM", None): -1,
    ("CONVERSION", None): 0,
}


def _decay(hours: Decimal) -> Decimal:
    return Decimal("0.5") ** (hours / HALF_LIFE_HOURS)


class WalletEquityEstimator:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        data_api: DataApiClient,
        collateral: CollateralReader,
        clock: Clock,
    ) -> None:
        self._sm = sm
        self._data = data_api
        self._collateral = collateral
        self._clock = clock

    async def snapshot(self, wallet: WatchedWallet) -> WalletEquitySnapshot:
        """Observe the wallet now; refresh its cached positions; persist the snapshot."""
        observed_at = self._clock.now()
        value: Decimal | None = None
        positions = None
        error: str | None = None
        try:
            value = await self._data.portfolio_value(wallet.address)
            positions = await self._data.positions(wallet.address)
        except UpstreamError as exc:
            error = str(exc)
        cash = await self._collateral.balance(wallet.address) if error is None else None
        pnl = None
        if error is None:
            try:
                pnl = await self._data.user_pnl(wallet.address)
            except UpstreamError as exc:
                log.info("user pnl unavailable", extra={"error": str(exc)})
        if value is None:
            coverage = EquityCoverage.UNAVAILABLE
        elif cash is None:
            coverage = EquityCoverage.POSITIONS_ONLY
        else:
            coverage = EquityCoverage.POSITIONS_AND_CASH
        row = WalletEquitySnapshot(
            wallet_id=wallet.id,
            observed_at=observed_at,
            positions_value=value,
            cash_balance=cash,
            coverage=coverage.value,
            open_positions=len(positions) if positions is not None else None,
            realized_pnl=pnl.realized if pnl else None,
            unrealized_pnl=pnl.unrealized if pnl else None,
            error=error,
        )
        async with self._sm() as s, s.begin():
            s.add(row)
            if positions is not None:
                await s.execute(delete(WalletPosition).where(WalletPosition.wallet_id == wallet.id))
                s.add_all(
                    WalletPosition(
                        wallet_id=wallet.id,
                        token_id=p.token_id,
                        condition_id=p.condition_id,
                        title=p.title,
                        outcome=p.outcome,
                        size=p.size,
                        avg_price=p.avg_price,
                        current_price=p.current_price,
                        current_value=p.current_value,
                        realized_pnl=p.realized_pnl,
                        unrealized_pnl=p.unrealized_pnl,
                        updated_at=observed_at,
                    )
                    for p in positions
                    if p.token_id
                )
        return row

    async def source_position_size(self, wallet: WatchedWallet, token_id: str) -> Decimal | None:
        """Fresh observed holding of ``token_id`` (0 if absent); ``None`` if unobservable."""
        try:
            positions = await self._data.positions(wallet.address)
        except UpstreamError:
            return None
        return next((p.size for p in positions if p.token_id == token_id), ZERO)

    point_in_time = False
    """When True (replay), never read snapshots observed after the current clock."""

    async def estimate_at(
        self, wallet_id: int, wallet: str, ts: datetime, *, exclude_event_id: int | None = None
    ) -> EquityEstimate:
        async with self._sm() as s:
            before = await s.scalar(
                select(WalletEquitySnapshot)
                .where(
                    WalletEquitySnapshot.wallet_id == wallet_id,
                    WalletEquitySnapshot.observed_at <= ts,
                    WalletEquitySnapshot.coverage != EquityCoverage.UNAVAILABLE.value,
                )
                .order_by(WalletEquitySnapshot.observed_at.desc())
                .limit(1)
            )
            after_q = select(WalletEquitySnapshot).where(
                WalletEquitySnapshot.wallet_id == wallet_id,
                WalletEquitySnapshot.observed_at > ts,
                WalletEquitySnapshot.coverage != EquityCoverage.UNAVAILABLE.value,
            )
            if self.point_in_time:
                # Replay: snapshots observed after the replay clock are future data.
                after_q = after_q.where(WalletEquitySnapshot.observed_at <= self._clock.now())
            after = await s.scalar(after_q.order_by(WalletEquitySnapshot.observed_at).limit(1))
            candidates = [c for c in (before, after) if c is not None]
            if not candidates:
                return unavailable(wallet, ts, self._clock.now(), "No usable equity snapshot.")
            snap = min(candidates, key=lambda c: abs((c.observed_at - ts).total_seconds()))
            lo, hi = sorted((snap.observed_at, ts))
            q = select(SourceEvent.type, SourceEvent.side, SourceEvent.usdc_size).where(
                SourceEvent.wallet_id == wallet_id,
                SourceEvent.block_ts >= lo if snap is after else SourceEvent.block_ts > lo,
                SourceEvent.block_ts <= hi if snap is after else SourceEvent.block_ts < hi,
            )
            if exclude_event_id is not None and snap is not after:
                q = q.where(SourceEvent.id != exclude_event_id)
            moves = (await s.execute(q)).all()

        net = ZERO
        for typ, side, usdc in moves:
            sign = _POSITION_SIGN.get((typ, side if typ == "TRADE" else None), 0)
            net += sign * usdc
        # Snapshot after the trade: undo moves in [ts, snap]; before: apply moves in (snap, ts).
        delta = -net if snap is after else net
        assert snap.positions_value is not None
        positions = max(ZERO, snap.positions_value + delta)
        cash = None if snap.cash_balance is None else max(ZERO, snap.cash_balance - delta)
        coverage = EquityCoverage(snap.coverage)
        hours = hours_between(snap.observed_at, ts)
        confidence = (BASE_CONFIDENCE[coverage] * _decay(hours)).quantize(Decimal("0.0001"))
        notes = [
            f"Snapshot {'after' if snap is after else 'before'} trade by "
            f"{int(hours * 60)} min; rolled by {len(moves)} recorded events."
        ]
        if coverage is EquityCoverage.POSITIONS_ONLY:
            notes.append("Cash balance unobservable: equity is a lower bound.")
        return EquityEstimate(
            wallet=wallet,
            as_of=ts,
            observed_at=snap.observed_at,
            positions_value=positions,
            cash_balance=cash,
            coverage=coverage,
            confidence=confidence,
            notes=tuple(notes),
        )
