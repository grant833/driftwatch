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
                  "triage, panel_assessments, predictions, llm_calls, fund_tickers, notifications, "
                  "controls, prices_daily, outcomes CASCADE")
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

    def call_json(self, model, system, user, schema, max_tokens=1024):
        import json

        from driftwatch.llm import ToolResult
        kind = "triage" if "items" in schema["properties"] else "panel"
        self.calls.append(kind)
        if kind == "triage":
            items = [json.loads(line) for line in user.splitlines()[1:]]
            return ToolResult({"items": [
                {"id": it["id"], "material": True, "category": "earnings",
                 # pretend the article is only "about" its first symbol
                 "relevant_tickers": it["symbols"][:1], "market_sentiment": 0.2}
                for it in items]}, 100, 50)
        return ToolResult({"p_up": 0.6, "magnitude": "Medium", "novelty": 0.7,
                           "confidence": 0.6, "rationale": "Beat and raise."}, 200, 60)


def test_analyst_pipeline_end_to_end(conn):
    from datetime import UTC, datetime

    from driftwatch import analyst

    now = datetime.now(UTC).isoformat()
    for i, syms in enumerate([["EXMP", "BYST"], ["VOO"], list("ABCDEFG"), ["ICLN"]], start=1):
        db.insert_news(conn, {"source": "t", "external_id": str(i), "headline": f"h{i}",
                              "summary": "s", "content": "<p>c</p>", "symbols": syms,
                              "url": None, "author": None, "published_at": now,
                              "updated_at": None, "raw": {"id": i}})
    cfg = {"max_news_age_hours": 6, "triage_model": "t", "panel_model": "p",
           "triage_batch_size": 20, "max_tickers_per_item": 2, "skip_if_more_symbols_than": 4,
           "personas": ["fundamental", "skeptic", "flow"]}
    llm = FakeLLM()
    assert analyst.run_triage(conn, llm, cfg) == 4
    funds = frozenset({"ICLN"})
    # EXMP qualifies; bystander BYST, blocklisted VOO, roundup, and ETF ICLN do not.
    made = sum(analyst.run_panel(conn, llm, cfg, frozenset({"VOO"}), funds) for _ in range(3))
    assert made == 1
    assert analyst.run_panel(conn, llm, cfg, frozenset({"VOO"}), funds) == 0  # no reprocessing
    assert llm.calls.count("panel") == 3  # 3 personas for EXMP only, nothing wasted

    p = conn.execute("SELECT ticker, p_up_mean, agree, stance FROM predictions").fetchall()
    assert len(p) == 1 and p[0][0] == "EXMP" and p[0][2] is True and p[0][3] == "agree"
    assert ledger.verify(conn)[0] is True
    assert analyst.calls_today(conn, "triage") == 1
    assert analyst.calls_today(conn, "panel") == 3
    assert conn.execute("SELECT count(*) FROM market_mood_hourly").fetchone()[0] == 1


# ---------------- Phase 1.2: prices, scorekeeper, notifier ----------------

class FakePrices:
    """Weekday bars for the last ~30 days; ABC beats SPY by 1% per day."""

    def __init__(self):
        from datetime import date, timedelta
        today = date.today()
        self.days = [today - timedelta(days=k) for k in range(30, 0, -1)
                     if (today - timedelta(days=k)).weekday() < 5]

    def snapshots(self, tickers):
        return {t: {"price": 103.0, "prev_close": 100.0, "trade_time": None} for t in tickers}

    def daily_bars(self, tickers, start, end):
        rows = []
        for i, d in enumerate(self.days):
            if start <= d <= end:
                rows.append(("SPY", d, 100.0, 100.0, 1e6))
                rows.append(("EXMP", d, 50.0 * 1.01 ** i, 50.0 * 1.01 ** (i + 1), 1e5))
        return [r for r in rows if r[0] in tickers]


def _make_prediction(conn, prices, edge=0.05):
    from datetime import UTC, datetime

    from driftwatch import analyst
    db.insert_news(conn, {"source": "t", "external_id": "x1", "headline": "Exmp beats and raises",
                          "summary": "s", "content": "<p>c</p>", "symbols": ["EXMP"],
                          "url": None, "author": None,
                          "published_at": datetime.now(UTC).isoformat(),
                          "updated_at": None, "raw": {}})
    cfg = {"max_news_age_hours": 6, "triage_model": "t", "panel_model": "p",
           "triage_batch_size": 20, "max_tickers_per_item": 2, "skip_if_more_symbols_than": 4,
           "personas": ["fundamental", "skeptic", "flow"]}
    llm = FakeLLM()
    analyst.run_triage(conn, llm, cfg)
    assert analyst.run_panel(conn, llm, cfg, frozenset(), frozenset(), prices, edge) == 1
    return llm


def test_price_context_and_signal_notification(conn):
    llm = FakeLLM()
    prices = FakePrices()
    _make_prediction(conn, prices)
    pre_move, ref = conn.execute("SELECT pre_move, ref_price FROM predictions").fetchone()
    assert abs(pre_move - 0.03) < 1e-9 and ref == 103.0
    kind, text = conn.execute("SELECT kind, text FROM notifications").fetchone()
    assert kind == "signal" and "EXMP" in text and "+3.0%" in text
    assert llm is not None


def test_scorer_end_to_end(conn):
    from datetime import datetime, time, timedelta

    from driftwatch import reports, scorer
    from driftwatch.prices import ET
    prices = FakePrices()
    _make_prediction(conn, prices)
    # Pretend the prediction was made at 11:00 ET on the 4th trading day of the window.
    made = datetime.combine(prices.days[3], time(11, 0), tzinfo=ET)
    conn.execute("UPDATE predictions SET created_at = %s", (made,))
    now = datetime.combine(prices.days[-1] + timedelta(days=1), time(9, 0), tzinfo=ET)
    assert scorer.refresh_prices(conn, prices, [1, 5, 10], now=now) > 0
    assert scorer.score_pending(conn, [1, 5, 10]) == 3
    assert scorer.score_pending(conn, [1, 5, 10]) == 0          # idempotent
    rows = conn.execute("SELECT horizon, entry_kind, excess FROM outcomes ORDER BY horizon")
    rows = rows.fetchall()
    assert [r[0] for r in rows] == [1, 5, 10] and rows[0][1] == "close"
    assert abs(rows[0][2] - 0.01) < 1e-9                         # +1% vs flat SPY
    card = scorer.scorecard(conn, [1, 5, 10])
    assert card[1]["hit_rate"] == 1.0 and card[1]["agree"]["n"] == 1
    text = reports.score(conn, [1, 5, 10])
    assert "hit rate" in text and "noise" in text


class FakeTG:
    def __init__(self, fail_status=None):
        self.sent, self.fail_status = [], fail_status

    def send(self, chat, text):
        from driftwatch.notifier import TelegramError
        if self.fail_status:
            raise TelegramError(self.fail_status, "nope")
        self.sent.append((chat, text))


def test_notifier_commands_and_lock(conn):
    from driftwatch import notifier
    upd = lambda uid, chat, text: {"update_id": uid, "message": {"chat": {"id": chat},  # noqa: E731
                                                                 "text": text}}
    tg = FakeTG()
    # No owner configured yet: bot only tells you your chat id.
    notifier.process_updates(conn, tg, None, [upd(1, 42, "/kill")], [1])
    assert "TELEGRAM_CHAT_ID=42" in tg.sent[0][1] and not notifier.is_halted(conn)
    # Strangers are ignored.
    tg = FakeTG()
    notifier.process_updates(conn, tg, 42, [upd(2, 99, "/kill")], [1])
    assert tg.sent == [] and not notifier.is_halted(conn)
    # Owner: /kill works instantly, /resume needs confirmation.
    notifier.process_updates(conn, tg, 42, [upd(3, 42, "/kill")], [1])
    assert notifier.is_halted(conn) and "HALTED" in tg.sent[-1][1]
    notifier.process_updates(conn, tg, 42, [upd(4, 42, "/resume")], [1])
    assert notifier.is_halted(conn)
    off = notifier.process_updates(conn, tg, 42, [upd(5, 42, "/resume confirm")], [1])
    assert not notifier.is_halted(conn) and off == 6
    assert notifier.get_control(conn, "telegram_offset") == "6"
    for cmd in ("/status", "/today", "/insiders", "/score", "/mood", "/costs", "/help"):
        notifier.process_updates(conn, tg, 42, [upd(10, 42, cmd)], [1, 5, 10])
        assert tg.sent[-1][1]
    assert "<b>driftwatch daily" in notifier.daily_summary(conn, [1, 5, 10])


def test_outbox_delivery_rules(conn):
    from datetime import UTC, datetime, timedelta

    from driftwatch import notifier
    from driftwatch.alerts import alert, notify
    notify(conn, "insider", "fresh")
    alert(None, "feed down", conn=conn)
    conn.execute("INSERT INTO notifications (kind, text, created_at) VALUES "
                 "('signal', 'stale', now() - interval '2 days')")
    tg = FakeTG()
    assert notifier.flush_outbox(conn, tg, 42) == 2
    assert [t for _, t in tg.sent] == ["fresh", "🚨 feed down"]          # stale skipped
    assert conn.execute("SELECT count(*) FROM notifications WHERE sent_at IS NULL"
                        ).fetchone()[0] == 0
    # Transient failure keeps the message queued; a 400 (bad message) drops it.
    notify(conn, "x", "retry me")
    with pytest.raises(notifier.TelegramError):
        notifier.flush_outbox(conn, FakeTG(fail_status=502), 42,
                              now=datetime.now(UTC) + timedelta(seconds=1))
    assert conn.execute("SELECT count(*) FROM notifications WHERE sent_at IS NULL"
                        ).fetchone()[0] == 1
    notifier.flush_outbox(conn, FakeTG(fail_status=400), 42)
    assert conn.execute("SELECT count(*) FROM notifications WHERE sent_at IS NULL"
                        ).fetchone()[0] == 0
