from datetime import UTC, date, datetime, timedelta

from driftwatch import fast_insider as fi

OPEN_NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)      # Thu 11:00 ET


class Prices:
    def __init__(self, age_min=1):
        self.age = age_min

    def snapshots(self, tickers):
        t = (OPEN_NOW - timedelta(minutes=self.age)).isoformat()
        return {"AAA": {"price": 10.0, "prev_close": 9.8, "trade_time": t},
                "IWM": {"price": 200.0, "prev_close": 199.0, "trade_time": t}}

    def daily_bars(self, tickers, start, end):
        days = [date(2026, 10, d) for d in (8, 9, 12, 13, 14, 15, 16)]
        rows = []
        for i, d in enumerate(days):
            rows.append(("AAA", d, 10 + 0.1 * i, 10.2 + 0.1 * i, 1))
            rows.append(("IWM", d, 200.0, 200.0, 1))
        return rows


def test_market_hours():
    assert fi.market_open(OPEN_NOW)
    assert not fi.market_open(datetime(2026, 10, 8, 21, 0, tzinfo=UTC))   # 5pm ET
    assert not fi.market_open(datetime(2026, 10, 10, 15, 0, tzinfo=UTC))  # Saturday


def test_record_and_grade(conn):
    assert fi.record(conn, Prices(), "X1", "AAA", OPEN_NOW)
    assert not fi.record(conn, Prices(), "X1", "AAA", OPEN_NOW)          # once per filing
    assert not fi.record(conn, Prices(age_min=60), "X2", "AAA", OPEN_NOW)  # stale quote
    assert not fi.record(conn, Prices(), "X3", "AAA",
                         datetime(2026, 10, 8, 22, 0, tzinfo=UTC))        # after hours
    n = fi.grade(conn, Prices(), now=datetime(2026, 10, 17, 23, 0, tzinfo=UTC))
    assert n == 1
    row = conn.execute("SELECT ex_close, ex_next_open, ex_1d, ex_5d, graded_at "
                       "FROM insider_fast").fetchone()
    assert abs(row[0] - 0.02) < 1e-9 and abs(row[1] - 0.01) < 1e-9
    assert abs(row[3] - (10.7 / 10 - 1)) < 1e-9 and row[4] is not None
    assert any("same-day close" in x and "n=1" in x for x in fi.report_lines(conn))
