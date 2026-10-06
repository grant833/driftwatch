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
        c.execute("DROP TABLE IF EXISTS ledger, news_items, filings, gdelt_tone CASCADE")
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
