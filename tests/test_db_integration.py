"""Runs against a real Postgres when TEST_DATABASE_URL is set (CI provides one)."""
import os

import psycopg
import pytest

from driftwatch import db, ledger

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("DRIFTWATCH_HOME", os.path.dirname(os.path.dirname(__file__)))
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS ledger, news_items, filings, gdelt_tone, insider_trades, "
                  "triage, panel_assessments, predictions, llm_calls CASCADE")
        db.apply_schema(c)
        yield c


def test_ledger_roundtrip_and_append_only(conn):
    for i in range(3):
        ledger.append(conn, "prediction", {"ticker": "ABC", "p_up": 0.55 + i / 100})
    assert ledger.verify(conn) == (True, None, 3)
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE ledger SET body = '{}' WHERE seq = 2")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM ledger WHERE seq = 3")


def test_news_insert_is_idempotent(conn):
    item = {"source": "t", "external_id": "1", "headline": "h", "summary": None,
            "content": None, "symbols": ["ABC"], "url": None, "author": None,
            "published_at": "2026-10-06T14:00:00Z", "updated_at": None, "raw": {"id": 1}}
    assert db.insert_news(conn, item) is True
    assert db.insert_news(conn, item) is False


class FakeLLM:
    """Deterministic stand-in for the Anthropic API."""

    def __init__(self):
        self.calls = []

    def call_tool(self, model, system, user, tool, max_tokens=1024):
        import json

        from driftwatch.llm import ToolResult
        self.calls.append(tool["name"])
        if tool["name"] == "record_triage":
            ids = [json.loads(line)["id"] for line in user.splitlines()[1:]]
            return ToolResult({"items": [{"id": i, "material": True, "category": "earnings",
                                          "market_sentiment": 0.2} for i in ids]}, 100, 50)
        return ToolResult({"p_up": 0.6, "magnitude": "medium", "novelty": 0.7,
                           "confidence": 0.6, "rationale": "Beat and raise."}, 200, 60)


def test_analyst_pipeline_end_to_end(conn):
    from datetime import UTC, datetime

    from driftwatch import analyst

    now = datetime.now(UTC).isoformat()
    for i, syms in enumerate([["EXMP"], ["VOO"], list("ABCDEFG")], start=1):
        db.insert_news(conn, {"source": "t", "external_id": str(i), "headline": f"h{i}",
                              "summary": "s", "content": "<p>c</p>", "symbols": syms,
                              "url": None, "author": None, "published_at": now,
                              "updated_at": None, "raw": {"id": i}})
    cfg = {"max_news_age_hours": 6, "triage_model": "t", "panel_model": "p",
           "triage_batch_size": 20, "max_tickers_per_item": 2, "skip_if_more_symbols_than": 4,
           "personas": ["fundamental", "skeptic", "flow"]}
    llm = FakeLLM()
    assert analyst.run_triage(conn, llm, cfg) == 3
    assert analyst.run_panel(conn, llm, cfg, frozenset({"VOO"})) == 1  # only EXMP qualifies
    assert analyst.run_panel(conn, llm, cfg, frozenset({"VOO"})) == 0  # nothing reprocessed

    p = conn.execute("SELECT ticker, p_up_mean, agree, ledger_seq FROM predictions").fetchall()
    assert len(p) == 1 and p[0][0] == "EXMP" and p[0][2] is True
    assert ledger.verify(conn)[0] is True
    assert analyst.calls_today(conn, "triage") == 1
    assert analyst.calls_today(conn, "panel") == 3
    assert conn.execute("SELECT count(*) FROM market_mood_hourly").fetchone()[0] == 1
