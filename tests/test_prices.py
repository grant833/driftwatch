from datetime import UTC, date, datetime

from driftwatch.prices import bar_day, completed_cutoff, parse_bars, parse_snapshots

SNAP = {"AAPL": {"latestTrade": {"t": "2026-10-06T14:00:00Z", "p": 210.0},
                 "prevDailyBar": {"c": 200.0}},
        "THIN": {"latestTrade": None, "prevDailyBar": {"c": 5.0}}}


def test_snapshots_both_layouts():
    out = parse_snapshots(SNAP)
    assert out["AAPL"]["price"] == 210.0 and out["AAPL"]["prev_close"] == 200.0
    assert "THIN" not in out
    assert parse_snapshots({"snapshots": SNAP}) == out


def test_bar_day_uses_eastern_date():
    assert bar_day("2026-10-06T04:00:00Z") == date(2026, 10, 6)
    rows = parse_bars({"bars": {"spy": [{"t": "2026-10-06T04:00:00Z", "o": 1, "c": 2, "v": 3}]}})
    assert rows == [("SPY", date(2026, 10, 6), 1.0, 2.0, 3.0)]


def test_completed_cutoff():
    # 3pm ET Oct 6 -> today's bar not final yet
    assert completed_cutoff(datetime(2026, 10, 6, 19, 0, tzinfo=UTC)) == date(2026, 10, 5)
    # 7pm ET Oct 6 -> today's bar is final
    assert completed_cutoff(datetime(2026, 10, 6, 23, 0, tzinfo=UTC)) == date(2026, 10, 6)
