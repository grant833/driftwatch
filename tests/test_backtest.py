import io
import zipfile
from datetime import date, timedelta

import pytest

from driftwatch import backtest as bt
from driftwatch import bt_data, ledger

CFG = {**bt.DEFAULTS, "horizons": [1, 5, 20]}


# ---------------- SEC data sets ----------------

def _tsv(header, rows):
    return "\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in rows)


def _zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", _tsv(
            ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK",
             "ISSUERTRADINGSYMBOL"],
            [["A1", "05-JAN-2024", "4", "123", "abc"],
             ["A2", "06-JAN-2024", "4", "456", "NONE"],
             ["A3", "07-JAN-2024", "3", "789", "XYZ"]]))
        z.writestr("NONDERIV_TRANS.tsv", _tsv(
            ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "TRANS_DATE", "TRANS_CODE",
             "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"],
            [["A1", "1", "03-JAN-2024", "P", "1000", "30.5", "A"],
             ["A1", "2", "03-JAN-2024", "S", "500", "31", "D"],        # a sale: ignored
             ["A2", "3", "04-JAN-2024", "P", "10", "1", "A"],
             ["A3", "4", "04-JAN-2024", "P", "10", "1", "A"]]))      # Form 3: ignored
        z.writestr("REPORTINGOWNER.tsv", _tsv(
            ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
             "RPTOWNER_TITLE"],
            [["A1", "9", "Jane Doe", "Director,Officer", "CEO"],
             ["A2", "8", "Fund LP", "TenPercentOwner", ""]]))
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_parse_quarter_keeps_only_form4_open_market_buys():
    trades, owners = bt_data.parse_quarter(_zip())
    assert [(t[0], t[1], t[2], t[5], t[9]) for t in trades] == [
        ("A1", 1, date(2024, 1, 5), "ABC", 30500.0), ("A2", 3, date(2024, 1, 6), None, 10.0)]
    assert owners[0] == ("A1", "9", "Jane Doe", True, True, "CEO")
    assert owners[1][3:5] == (False, False)


def test_clean_ticker_variants():
    assert bt_data.clean_ticker("BRK/B") == "BRK.B"
    assert bt_data.clean_ticker("GOOG/GOOGL") == "GOOG"
    assert bt_data.clean_ticker("abc, def") == "ABC"


def test_top_titles():
    assert bt.TOP_TITLE.search("PRESIDENT AND CEO")
    assert bt.TOP_TITLE.search("CHAIRMAN OF THE BOARD")
    assert not bt.TOP_TITLE.search("SENIOR VICE PRESIDENT, SALES")
    assert not bt.TOP_TITLE.search("VICE-PRESIDENT")


def test_helpers():
    assert bt_data.quarters("2025q3", date(2026, 2, 1)) == ["2025q3", "2025q4"]
    assert bt_data.quarter_bounds("2024q4") == (date(2024, 10, 1), date(2024, 12, 31))
    assert bt_data.sec_date("05-JAN-2024") == date(2024, 1, 5)
    assert bt_data.clean_ticker(" brk-b ") == "BRK.B" and bt_data.clean_ticker("N/A") is None
    w = bt_data.merge_windows([date(2024, 1, 1), date(2024, 2, 1), date(2025, 6, 1)])
    assert len(w) == 2 and bt_data.in_windows(date(2024, 3, 1), w)


# ---------------- events ----------------

def _cal(n=80, start=date(2024, 1, 1)):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars(cal, prices):
    """prices: list of (open, low, close) per calendar day, or a constant."""
    return {d: p for d, p in zip(cal, prices, strict=False)}


def test_entry_is_the_open_after_the_filing_date_never_earlier():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    px = [(10, 10, 10)] * 50 + [(11, 11, 12)] + [(12, 12, 12)] * 29
    sig = bt.Signal("AAA", cal[49], cal[47], 1, 50000, True, True)      # filed on day 49
    ev, why = bt.build_event(sig, cal, _bars(cal, px), spy, CFG)
    assert why == "" and ev.entry_idx == 50 and ev.path[0][1] == 11      # day 50's open
    assert ev.ret[1] == pytest.approx(12 / 11 - 1)                       # same-day close
    assert ev.spy[1] == 0 and ev.ref_close == 10
    assert not bt.chase_ok(11, 10, 0.05) and bt.chase_ok(10.4, 10, 0.05)


def test_volatility_and_beta_use_only_bars_before_entry():
    cal = _cal()
    spy = _bars(cal, [(100 + i % 3, 0, 100 + i % 3) for i in range(len(cal))])
    calm = [(50 + i % 3 / 2, 0, 50 + i % 3 / 2) for i in range(50)]
    wild = [(5, 5, 5 * (1 + (i % 2))) for i in range(30)]               # crazy after entry
    ev, _ = bt.build_event(bt.Signal("AAA", cal[49], None, 1, 5e4, True, False), cal,
                           _bars(cal, calm + wild), spy, CFG)
    assert ev.vol is not None and ev.vol < 0.05
    assert ev.beta == pytest.approx(1.0, abs=0.05)


def test_missing_bars_and_delisting():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    px = {d: (10, 10, 10) for d in cal[:55]}                             # stops after day 54
    ev, _ = bt.build_event(bt.Signal("AAA", cal[49], None, 1, 5e4, True, False), cal, px,
                           spy, CFG)
    assert ev.delisted and ev.ret[20] == 0.0                             # last close carried
    ev2, why = bt.build_event(bt.Signal("BBB", cal[49], None, 1, 5e4, True, False), cal, {},
                              spy, CFG)
    assert ev2 is None and why == "no price data"
    ev3, why = bt.build_event(bt.Signal("CCC", cal[-1], None, 1, 5e4, True, False), cal, px,
                              spy, CFG)
    assert why == "too recent"


def test_study_counts_crowded_months_once():
    def ev(month, x):
        e = bt.Event(bt.Signal("A", date(2024, month, 3), None, 1, 1, True, False), 0, None,
                     None, 1.0)
        e.ret, e.spy = {20: x}, {20: 0.0}
        return e
    events = [ev(1, 0.02)] * 10 + [ev(2, -0.01), ev(3, 0.01), ev(4, 0.03)]
    s = bt.study(events, 20)
    assert s["n"] == 13 and s["n_months"] == 4 and s["hit_rate"] == pytest.approx(12 / 13)
    assert s["mean_abnormal"] == pytest.approx(s["mean_excess"])


# ---------------- portfolio ----------------

def _event(cal, idx, path_prices, ticker="AAA", n=1, value=50000, ref=None, vol=0.02):
    path = [(idx + k, *p) for k, p in enumerate(path_prices)]
    return bt.Event(bt.Signal(ticker, cal[idx - 1], None, n, value, True, False), idx, ref,
                    vol, 1.0, path)


def test_simulation_time_exit_after_20_sessions_with_costs():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    e = _event(cal, 10, [(10, 10, 10)] * 20 + [(11, 11, 11)] * 6)
    sim = bt.simulate([e], cal, spy, {**CFG, "cost_bps": 0})
    (t,) = sim["trades"]
    assert t["why"] == "time" and t["entry"] == cal[10].isoformat()
    assert t["exit"] == cal[30].isoformat() and t["ret"] == pytest.approx(0.10)
    # sized at 1/12 of equity (vol equals the reference vol), integer shares
    assert sim["curve"][-1][1] == pytest.approx(100000 + 833 * 1.0)


def test_simulation_stop_loss_and_chase_rule():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    fall = _event(cal, 10, [(10, 10, 10), (10, 9, 9.5), (9.5, 8.5, 8.9)] + [(9, 9, 9)] * 20)
    ran = _event(cal, 10, [(12, 12, 12)] * 26, ticker="RUN", ref=10.0)
    sim = bt.simulate([fall, ran], cal, spy, {**CFG, "cost_bps": 0})
    assert [(t["ticker"], t["why"]) for t in sim["trades"]] == [("AAA", "stop")]
    assert sim["trades"][0]["exit"] == cal[12].isoformat()


def test_simulation_respects_slots_and_ranking():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    events = [_event(cal, 10, [(10, 10, 10)] * 26, ticker=f"T{i:02d}", n=1 + (i == 7))
              for i in range(15)]
    sim = bt.simulate(events, cal, spy, {**CFG, "cost_bps": 0})
    stats_trades = sim["trades"]
    assert len(stats_trades) == 12                       # 12 slots
    assert "T07" in {t["ticker"] for t in stats_trades}  # the cluster buy ranked first


def test_risk_off_halves_the_slots():
    cal = _cal(260)
    falling = {d: (300 - i,) * 3 for i, d in enumerate(cal)}          # SPY below its average
    events = [_event(cal, 210, [(10, 10, 10)] * 26, ticker=f"T{i:02d}") for i in range(15)]
    sim = bt.simulate(events, cal, falling, {**CFG, "cost_bps": 0})
    assert len(sim["trades"]) == 6


def test_daily_loss_brake_blocks_new_entries():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    crash = _event(cal, 10, [(10, 10, 10), (10, 10, 10), (5, 5, 9.5)] + [(9.5,) * 3] * 23,
                   ticker="AAA")
    big = {**CFG, "cost_bps": 0, "max_positions": 1, "max_position_pct": 1.0,
           "max_invested": 1.0, "stop_loss": 0.9}
    later = _event(cal, 12, [(10, 10, 10)] * 26, ticker="BBB")
    sim = bt.simulate([crash, later], cal, spy, {**big, "max_positions": 2,
                                                  "max_position_pct": 0.9,
                                                  "halt_drawdown": 0.99,
                                                  "lookback_sessions": 1})
    assert "BBB" not in {t["ticker"] for t in sim["trades"]}             # blocked that day
    assert sim["blocked_days"] == 1 and sim["drawdown_halts"] == 0
    calm = bt.simulate([later], cal, spy, {**big, "lookback_sessions": 1})
    assert [t["ticker"] for t in calm["trades"]] == ["BBB"]             # otherwise bought


def test_drawdown_halt_pauses_entries_then_resumes():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    crash = _event(cal, 10, [(10, 10, 10), (7, 7, 7)] + [(7,) * 3] * 24, ticker="AAA")
    cfg = {**CFG, "cost_bps": 0, "max_positions": 2, "max_position_pct": 0.6,
           "stop_loss": 0.9, "halt_daily_loss": 0.99}
    blocked = _event(cal, 12, [(10, 10, 10)] * 26, ticker="BBB")
    after = _event(cal, 20, [(10, 10, 10)] * 26, ticker="CCC")
    sim = bt.simulate([crash, blocked, after], cal, spy, {**cfg, "lookback_sessions": 1})
    names = {t["ticker"] for t in sim["trades"]}
    assert sim["drawdown_halts"] == 1 and "BBB" not in names and "CCC" in names


def test_portfolio_stats_and_matched_benchmark():
    cal = _cal(300)
    spy = {d: (100 * 1.001 ** i,) * 3 for i, d in enumerate(cal)}
    sim = {"curve": [(d, 100000 * 1.0005 ** i, spy[d][2]) for i, d in enumerate(cal)],
           "trades": [{"ret": 0.05, "why": "time"}, {"ret": -0.02, "why": "stop"}],
           "exposure": [0.5] * len(cal)}
    p = bt.portfolio_stats(sim)
    assert p["cagr"] < p["spy_cagr"]
    assert p["matched_spy_cagr"] == pytest.approx(p["cagr"], rel=0.05)  # half of SPY's drift
    assert p["win_rate"] == 0.5 and p["exits"] == {"stop": 1, "time": 1}
    assert set(p["yearly"]) == {cal[0].year, cal[-1].year}


# ---------------- end to end (database) ----------------

def test_run_end_to_end(conn, tmp_path):
    cal = _cal(400, date(2016, 1, 4))
    q = bt_data.quarter_of(cal[190])
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO bt_bars VALUES ('SPY', 'SPY', %s, 100, 100, 100)",
                        [(d,) for d in cal])
        cur.executemany("INSERT INTO bt_bars VALUES (%s, 'GOOD', %s, %s, %s, %s)",
                        [(q, d, 10 + (i > 200), 10 + (i > 200), 10 + (i > 200))
                         for i, d in enumerate(cal)])
        # a different fetch batch with another adjustment basis must never be mixed in
        cur.executemany("INSERT INTO bt_bars VALUES ('2015q1', 'GOOD', %s, 1, 1, 1)",
                        [(d,) for d in cal])
    for acc, who, officer, title in [("A1", "1", True, "CEO"), ("A2", "2", False, None)]:
        conn.execute("INSERT INTO bt_insider_trades VALUES (%s, 1, %s, '4', '77', 'GOOD', %s, "
                     "10000, 10, 100000)", (acc, cal[190], cal[189] - timedelta(days=acc == "A2")))
        conn.execute("INSERT INTO bt_insider_owners VALUES (%s, %s, 'x', %s, true, %s)",
                     (acc, who, officer, title))
    # the same purchase reported again by an affiliated filer: counted once
    conn.execute("INSERT INTO bt_insider_trades VALUES ('A4', 1, %s, '4', '77', 'GOOD', %s, "
                 "10000, 10, 100000)", (cal[190], cal[189]))
    conn.execute("INSERT INTO bt_insider_owners VALUES ('A4', '1', 'x', true, true, 'CEO')")
    conn.execute("INSERT INTO bt_insider_owners VALUES ('A4', '4', 'trust', false, true, null)")
    # two different people buying identical lots the same day are NOT duplicates
    conn.execute("INSERT INTO bt_insider_trades VALUES ('A6', 1, %s, '4', '77', 'GOOD', %s, "
                 "10000, 10, 100000)", (cal[190], cal[189]))
    conn.execute("INSERT INTO bt_insider_owners VALUES ('A6', '6', 'w', false, true, null)")
    # a pre-planned (10b5-1) purchase never counts
    conn.execute("INSERT INTO bt_insider_trades VALUES ('A5', 1, %s, '4', '77', 'GOOD', %s, "
                 "500, 10, 5000000, true)", (cal[190], cal[189]))
    conn.execute("INSERT INTO bt_insider_owners VALUES ('A5', '5', 'z', true, true, 'CFO')")
    conn.execute("INSERT INTO bt_insider_trades VALUES ('A3', 1, %s, '4', '88', 'FUND', %s, "
                 "10000, 10, 100000)", (cal[190], cal[189]))
    conn.execute("INSERT INTO bt_insider_owners VALUES ('A3', '3', 'y', true, true, 'CEO')")
    conn.execute("INSERT INTO sec_companies (cik, sic) VALUES ('0000000088', '6726')")
    r = bt.run(conn, tmp_path)
    assert r["data"]["signals"] == 1
    assert r["data"]["skipped_signals"] == {"fund or SPAC (SEC industry code)": 1}
    s = r["event_study"]["all signals (pre-registered)"]["20"]
    assert s["n"] == 1 and s["median_excess"] == pytest.approx(0.1)
    sig = bt.load_signals(conn, bt.DEFAULTS)[0][0]
    assert (sig.n_insiders, sig.value, sig.top_officer) == (4, 300000, True)
    kinds = [r_[0] for r_ in conn.execute("SELECT kind FROM ledger ORDER BY seq")]
    assert kinds == ["backtest_registered", "backtest_result"]
    assert ledger.verify(conn)[0]
    assert (tmp_path / "docs" / "data" / "backtest.json").exists()
    assert "Pre-registered" in next((tmp_path / "backtests").iterdir()).read_text()


def test_pool_counts_people_not_filings():
    cal = _cal()
    spy = _bars(cal, [(100, 100, 100)] * len(cal))
    one = [_event(cal, 10, [(10, 10, 10)] * 26, ticker="SOLO") for _ in range(3)]
    for e in one:
        e.sig.owners = frozenset({"1"})                  # same person, three filings
    two = _event(cal, 10, [(10, 10, 10)] * 26, ticker="DUO")
    two.sig.owners, two.sig.n_insiders = frozenset({"2", "3"}), 2
    sim = bt.simulate(one + [two], cal, spy, {**CFG, "cost_bps": 0, "max_positions": 1,
                                              "max_position_pct": 0.5})
    assert [t["ticker"] for t in sim["trades"]][0] == "DUO"
