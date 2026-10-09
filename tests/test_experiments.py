from datetime import date

from driftwatch import bt_diagnose, db, experiments, ledger
from driftwatch.backtest import Signal


def _call(conn, tkr, p, pre_move, category, excess, day):
    db.insert_news(conn, {"source": "t", "external_id": tkr, "headline": "h", "summary": None,
                          "content": None, "symbols": [tkr], "url": None, "author": None,
                          "published_at": "2026-10-01T12:00:00+00:00", "updated_at": None,
                          "raw": {}})
    nid = conn.execute("SELECT id FROM news_items WHERE external_id = %s", (tkr,)).fetchone()[0]
    e = ledger.append(conn, "prediction", {"t": tkr})
    conn.execute("INSERT INTO predictions (news_id, ticker, ledger_seq, p_up_mean, p_up_std, "
                 "novelty_mean, agree, magnitude, stance, pre_move) VALUES "
                 "(%s,%s,%s,%s,0,0.5,true,'small','agree',%s)", (nid, tkr, e.seq, p, pre_move))
    conn.execute("INSERT INTO triage (news_id, material, category, model, prompt_version) "
                 "VALUES (%s, true, %s, 'm', 'v')", (nid, category))
    for h in (1, 5):
        conn.execute("INSERT INTO outcomes (news_id, ticker, horizon, entry_day, entry_kind, "
                     "entry_px, exit_day, exit_px, ret, spy_ret, excess) VALUES "
                     "(%s,%s,%s,%s,'open',1,%s,1,0,0,%s)", (nid, tkr, h, day, day, excess))


def test_experiments_report_groups_calls(conn):
    _call(conn, "AAA", 0.6, 0.02, "earnings", 0.03, date(2026, 10, 1))    # hard, agrees
    _call(conn, "BBB", 0.6, -0.02, "product", -0.01, date(2026, 10, 2))   # soft, fights
    _call(conn, "CCC", 0.4, -0.03, "guidance", -0.02, date(2026, 10, 3))  # hard, agrees
    text = experiments.report(conn, [1, 5])
    assert "hard news" in text and "n=2" in text
    assert "agrees with reaction" in text
    cal = dict((b, (n, s)) for b, n, s in experiments.calibration(conn, 5))
    assert cal["0.55-0.60"] == (0, None) and cal["0.60-1.00"][0] == 2


def test_diagnostic_legs_never_overlap_and_use_the_right_prices():
    cal = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13)]
    legs = bt_diagnose.legs_for(date(2026, 1, 8), date(2026, 1, 6), cal, {})
    assert legs[bt_diagnose.LEGS[0]] == ((date(2026, 1, 6), 2), (date(2026, 1, 8), 2))
    assert legs[bt_diagnose.LEGS[1]] == ((date(2026, 1, 8), 2), (date(2026, 1, 9), 0))
    assert legs[bt_diagnose.LEGS[2]] == ((date(2026, 1, 9), 0), (date(2026, 1, 9), 2))
    assert bt_diagnose.LEGS[4] not in legs                      # not enough sessions yet


def test_diagnose_run(conn, tmp_path, monkeypatch):
    days = [date(2024, 1, 2 + i) for i in range(28) if date(2024, 1, 2 + i).weekday() < 5]
    with conn.cursor() as cur:
        for b in ("SPY", "IWM"):
            cur.executemany("INSERT INTO bt_bars VALUES (%s, %s, %s, 100, 100, 100)",
                            [(b, b, d) for d in days])
        cur.executemany("INSERT INTO bt_bars VALUES ('2024q1', 'AAA', %s, %s, %s, %s)",
                        [(d, 10 + i, 10 + i, 10 + i) for i, d in enumerate(days)])
    monkeypatch.setattr(bt_diagnose, "load_signals", lambda c, cfg: (
        [Signal("AAA", days[3], days[1], 1, 5e4, True, False)], {}))
    out = bt_diagnose.run(conn, tmp_path)
    a = out["table"]["SPY"][bt_diagnose.LEGS[0]]
    assert a["n"] == 1 and abs(a["median"] - (13 / 11 - 1)) < 1e-9
    assert "vs IWM" in (tmp_path / "backtests").iterdir().__next__().read_text()
