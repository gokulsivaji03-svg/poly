"""Copy quality: premium paid over the source's fill, detect-to-fill time and skip reasons."""

from datetime import timedelta
from decimal import Decimal

from app.api.queries import copy_quality_of, premium
from app.db.models import CopySignal, SignalStatus

from factories import NOW

D = Decimal


def signal(status, side="BUY", source="0.40", fill=None, ms=None, reason=None, note=False):
    items = [{"code": "SIZING_NOTE", "message": ""}] if note else []
    return CopySignal(
        status=status,
        side=side,
        source_price=D(source),
        avg_fill_price=D(fill) if fill else None,
        detected_at=NOW,
        executed_at=NOW + timedelta(milliseconds=ms) if ms is not None else None,
        reasons={"items": items + ([{"code": reason, "message": ""}] if reason else [])},
    )


def test_premium_is_what_the_copy_paid_beyond_the_source_on_either_side():
    assert premium("BUY", D("0.40"), D("0.41")) == D("0.01")
    assert premium("SELL", D("0.60"), D("0.59")) == D("0.01")
    assert premium("SELL", D("0.60"), D("0.61")) == D("-0.01"), "a better exit is negative"
    assert premium("BUY", D("0.40"), None) is None


def test_quality_summarises_fills_skips_and_failures():
    q = copy_quality_of(
        [
            signal(SignalStatus.EXECUTED, fill="0.40", ms=8),
            signal(SignalStatus.EXECUTED, fill="0.41", ms=12),
            signal(SignalStatus.PARTIAL, side="SELL", source="0.60", fill="0.58", ms=30),
            signal(SignalStatus.SKIPPED, reason="BELOW_MIN_ORDER"),
            signal(SignalStatus.REJECTED, reason="BELOW_MIN_ORDER", note=True),
            signal(SignalStatus.REJECTED, reason="PRICE_ABOVE_CAP"),
            signal(SignalStatus.FAILED),
        ],
        24,
    )
    assert (q["signals"], q["filled"], q["skipped"], q["failed"]) == (7, 3, 3, 1)
    assert (D(q["premium_p50"]), D(q["premium_p90"])) == (D("0.01"), D("0.02"))
    assert q["at_source_share"] == "0.3333"
    assert (q["detect_to_fill_ms_p50"], q["detect_to_fill_ms_p99"]) == (12, 30)
    assert q["skip_reasons"] == [
        {"code": "BELOW_MIN_ORDER", "n": 2},
        {"code": "PRICE_ABOVE_CAP", "n": 1},
    ], "a skip's cause is its last reason, not the note before it"


def test_no_fills_leaves_the_fill_numbers_unknown_rather_than_zero():
    q = copy_quality_of([signal(SignalStatus.SKIPPED, reason="PAUSED")], 1)
    assert q["premium_p50"] is None and q["at_source_share"] is None
    assert q["detect_to_fill_ms_p50"] is None
