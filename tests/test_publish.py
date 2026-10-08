import json
import math
from datetime import UTC, date, datetime, timedelta

import pytest

from driftwatch import db, ledger, publish

# ---------------- pure ----------------

def test_clean_makes_json_safe():
    out = publish.clean({"a": math.nan, "b": [math.inf, 1.5], 3: date(2026, 10, 8)})
    assert out == {"a": None, "b": [None, 1.5], "3": "2026-10-08"}
    json.dumps(out, allow_nan=False)


def test_est_cost_uses_list_prices():
    assert publish.est_cost("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert publish.est_cost("unknown-model", 10, 10) is None


def test_stale_publish_detection():
    now = datetime(2026, 10, 10, 18, tzinfo=UTC)
    assert publish.stale(now, None) is False                       # never set up: not a fault
    assert publish.stale(now, (now - timedelta(hours=24)).isoformat()) is False
    assert publish.stale(now, (now - timedelta(hours=60)).isoformat()) is True


# ---------------- database ----------------

def _prediction(conn, ticker, p, headline="Secret licensed headline text"):
    db.insert_news(conn, {"source": "t", "external_id": ticker, "headline": headline,
                          "summary": "s", "content": "c", "symbols": [ticker],
                          "url": f"https://example.com/{ticker}", "author": None,
                          "published_at": datetime.now(UTC).isoformat(), "updated_at": None,
                          "raw": {}})
    nid = conn.execute("SELECT id FROM news_items WHERE external_id = %s", (ticker,)).fetchone()[0]
    e = ledger.append(conn, "prediction", {"ticker": ticker, "headline": headline})
    conn.execute("INSERT INTO predictions (news_id, ticker, ledger_seq, p_up_mean, p_up_std, "
                 "novelty_mean, agree, magnitude, stance) VALUES "
                 "(%s,%s,%s,%s,0.02,0.5,true,'small','agree')", (nid, ticker, e.seq, p))
    return nid


def test_run_writes_anchor_and_dashboard_data(conn, tmp_path):
    nid = _prediction(conn, "AAA", 0.62)
    _prediction(conn, "BBB", 0.40)
    conn.execute("INSERT INTO outcomes (news_id, ticker, horizon, entry_day, entry_kind, "
                 "entry_px, exit_day, exit_px, ret, spy_ret, excess) VALUES "
                 "(%s,'AAA',1,'2026-10-07','open',10,'2026-10-07',11,0.1,0.01,0.09)", (nid,))
    for d, eq, spy in [("2026-10-06", 100000, 600), ("2026-10-07", 101000, 603),
                       ("2026-10-08", 100500, 601)]:
        conn.execute("INSERT INTO equity_daily (account, day, equity, spy_close) "
                     "VALUES ('news', %s, %s, %s)", (d, eq, spy))
    conn.execute("INSERT INTO llm_calls (stage, model, input_tokens, output_tokens) "
                 "VALUES ('panel', 'claude-sonnet-5-5', 1000000, 0)")
    now = datetime(2026, 10, 8, 21, 30, tzinfo=UTC)               # 5:30 PM ET

    paths = publish.run(conn, tmp_path, [1, 5, 10], 3, now=now)

    anchor = (tmp_path / "anchors" / "2026-10-08.txt").read_text()
    assert f"hash={ledger.head(conn).hash}" in anchor
    assert len(paths) == 6
    data = tmp_path / "docs" / "data"
    summary = json.loads((data / "summary.json").read_text())
    assert summary["ledger"]["verified"] is True and summary["ledger"]["entries"] == 2
    assert summary["predictions_total"] == 2
    assert summary["costs"]["total_30d_usd"] == pytest.approx(2.0)
    assert summary["tournament"]["accounts"]["news"]["days"] == 3
    assert "r" not in summary["tournament"]["accounts"]["news"]
    preds = json.loads((data / "predictions.json").read_text())
    aaa = next(p for p in preds if p["ticker"] == "AAA")
    assert aaa["excess"] == {"1": 0.09} and aaa["url"] == "https://example.com/AAA"
    everything = "".join(p.read_text() for p in data.iterdir())
    assert "Secret licensed headline" not in everything            # headlines never published
    assert db.get_control(conn, "last_publish") == now.isoformat()


def test_run_refuses_and_alerts_when_chain_is_broken(conn, tmp_path):
    _prediction(conn, "AAA", 0.62)
    _prediction(conn, "BBB", 0.40)
    conn.execute("ALTER TABLE ledger DISABLE TRIGGER USER")        # simulate tampering
    conn.execute("UPDATE ledger SET body = '{\"edited\":true}' WHERE seq = 1")
    conn.execute("ALTER TABLE ledger ENABLE TRIGGER USER")
    with pytest.raises(RuntimeError, match="seq 1"):
        publish.run(conn, tmp_path, [1, 5, 10], 3)
    assert not (tmp_path / "docs").exists() and not (tmp_path / "anchors").exists()
    text = conn.execute("SELECT text FROM notifications ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "BROKEN" in text


def test_publish_with_empty_database(conn, tmp_path):
    paths = publish.run(conn, tmp_path, [1, 5, 10], 3)
    assert len(paths) == 5                                          # no anchor for an empty ledger
    summary = json.loads((tmp_path / "docs" / "data" / "summary.json").read_text())
    assert summary["ledger"]["head_hash"] is None
