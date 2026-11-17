from app.core.stages import Stage, StageTimer, spans, summarize


def test_spans_are_measured_from_the_latest_earlier_mark():
    rows = [
        {"taped": 100, "started": 150, "context": 400, "decided": 410},
        {"taped": 200, "context": 300},
    ]
    summary = summarize(rows)
    assert summary["taped"].p50 == 150
    assert summary["started"].n == 1 and summary["started"].p50 == 50
    assert sorted([summary["context"].max, summary["context"].p50]) == [175, 250]
    assert summary["total"].max == 410
    assert "reserved" not in summary


def test_one_copy_reports_its_own_stage_durations_and_total():
    row = {"received": 0, "taped": 100, "context": 400, "decided": 410}
    assert spans(row) == {"taped": 100, "context": 300, "decided": 10, "total": 410}
    assert spans({}) == {}


def test_fork_keeps_origin_and_earlier_marks_but_not_later_ones():
    timer = StageTimer()
    timer.mark(Stage.TAPED)
    a, b = timer.fork(), timer.fork()
    a.mark(Stage.STARTED)
    assert set(a.as_us()) == {"taped", "started"} and set(b.as_us()) == {"taped"}
    assert a.origin_ns == b.origin_ns == timer.origin_ns
