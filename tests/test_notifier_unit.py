from datetime import UTC, datetime

from driftwatch.notifier import clip, summary_due


def test_summary_due():
    tue_445pm = datetime(2026, 10, 6, 20, 45, tzinfo=UTC)   # 4:45pm ET
    assert summary_due(tue_445pm, "16:45", None)
    assert not summary_due(tue_445pm, "16:45", "2026-10-06")   # already sent today
    assert not summary_due(tue_445pm, "17:00", None)           # too early
    sat = datetime(2026, 10, 10, 21, 0, tzinfo=UTC)
    assert not summary_due(sat, "16:45", None)                 # weekends off


def test_clip():
    assert clip("x" * 5000).endswith("(truncated)") and len(clip("x" * 5000)) <= 4000
