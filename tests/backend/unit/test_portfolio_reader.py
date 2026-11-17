"""Day P&L baseline: read once per UTC day, never stale across midnight."""

from decimal import Decimal

from app.core.clock import ManualClock
from app.engine.portfolio import PortfolioReader

from factories import NOW


class FakeLedger:
    def __init__(self, start: Decimal | None):
        self.start = start
        self.reads = 0

    async def day_start_equity(self, account_id, day_start):
        self.reads += 1
        return self.start


async def test_day_start_equity_is_read_once_per_utc_day():
    clock = ManualClock(NOW)
    ledger = FakeLedger(Decimal("1000"))
    reader = PortfolioReader(ledger, None, clock, 1)  # type: ignore[arg-type]
    assert await reader._day_start_equity() == Decimal("1000")
    clock.advance(3600)
    assert await reader._day_start_equity() == Decimal("1000")
    assert ledger.reads == 1
    clock.advance(24 * 3600)
    ledger.start = Decimal("1200")
    assert await reader._day_start_equity() == Decimal("1200"), "new day, new baseline"
    assert ledger.reads == 2


async def test_a_day_without_a_snapshot_is_not_cached():
    clock = ManualClock(NOW)
    ledger = FakeLedger(None)
    reader = PortfolioReader(ledger, None, clock, 1)  # type: ignore[arg-type]
    assert await reader._day_start_equity() is None
    ledger.start = Decimal("900")
    assert await reader._day_start_equity() == Decimal("900")
