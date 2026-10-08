"""End-to-end trading tests against Postgres with a simulated broker and market."""
from datetime import UTC, date, datetime, timedelta

from driftwatch import db, ledger, notifier, perf, reports, trader
from driftwatch.broker import Account, BrokerError, Position
from driftwatch.db import get_control, set_control
from driftwatch.prices import ET

TCFG = {"limit_slippage": 0.005, "max_invested": 0.95, "min_price": 5.0, "halt_drawdown": 0.15,
        "halt_daily_loss": 0.03, "risk_off_slots": 0.5, "default_daily_vol": 0.03,
        "reference_daily_vol": 0.02}
NEWS = {"strategy": "news_drift", "max_positions": 10, "max_position_pct": 0.10, "hold_days": 5,
        "stop_loss": 0.08, "min_edge": 0.05, "max_pre_move": 0.05, "max_chase": 0.03,
        "entry_window_hours": 24}
INSIDER = {"strategy": "insider_follow", "max_positions": 10, "max_position_pct": 0.10,
           "hold_days": 20, "stop_loss": 0.10, "lookback_days": 3, "max_chase": 0.05}
TREND = {"strategy": "spy_trend", "sma_days": 200}
NOW = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)          # Thu 10:00 ET
TODAY = NOW.astimezone(ET).date()


class FakeBroker:
    def __init__(self, equity=100_000.0, last=None, cash=None, positions=None):
        self.acct = Account(equity, last or equity, cash if cash is not None else equity, False)
        self.pos = positions or []
        self.sent, self.status, self.cancelled = [], {}, 0

    def account(self):
        return self.acct

    def positions(self):
        return list(self.pos)

    def clock(self):
        raise NotImplementedError

    def submit(self, ticker, side, qty, limit_price, client_order_id):
        if any(s[4] == client_order_id for s in self.sent):
            raise BrokerError(422, "client_order_id must be unique")
        self.sent.append((ticker, side, qty, limit_price, client_order_id))
        self.status[client_order_id] = ("new", 0, None)
        return {"id": f"b-{len(self.sent)}", "status": "new"}

    def order(self, coid):
        if coid not in self.status:
            return None
        st, q, p = self.status[coid]
        return {"id": "x", "client_order_id": coid, "status": st, "filled_qty": str(q),
                "filled_avg_price": str(p) if p else None}

    def cancel_open(self):
        self.cancelled += 1


class FakeMarket:
    """Daily bars for any ticker: SPY trending per `spy_up`; others steady at `px`."""

    def __init__(self, px=None, spy_up=True, trade_day=TODAY):
        self.px = px or {}
        self.spy_up = spy_up
        self.trade_day = trade_day

    def snapshots(self, tickers):
        out = {}
        for t in tickers:
            p = 500.0 if t == "SPY" else self.px.get(t)
            if p:
                out[t] = {"price": p, "prev_close": p,
                          "trade_time": f"{self.trade_day}T19:59:00Z"}
        return out

    def daily_bars(self, tickers, start, end):
        rows, d, i = [], start, 0
        while d <= end:
            if d.weekday() < 5:
                for t in tickers:
                    if t == "SPY":
                        c = 300 + i if self.spy_up else 600 - i
                    else:
                        c = self.px.get(t, 50.0) * (1 + 0.01 * (-1) ** i)
                    rows.append((t, d, c, c, 1e6))
                i += 1
            d += timedelta(days=1)
        return rows


def acct(name, cfg, broker):
    return trader.Acct(name, cfg, broker)


def news_signal(conn, ticker, p=0.62, ref=100.0, pre=0.01, stance="agree", hours_ago=1):
    db.insert_news(conn, {"source": "t", "external_id": f"{ticker}{p}{hours_ago}",
                          "headline": f"{ticker} beats and raises", "summary": None,
                          "content": None, "symbols": [ticker], "url": None, "author": None,
                          "published_at": datetime.now(UTC).isoformat(), "updated_at": None,
                          "raw": {}})
    nid = conn.execute("SELECT max(id) FROM news_items").fetchone()[0]
    e = ledger.append(conn, "prediction", {"t": ticker})
    conn.execute("INSERT INTO predictions (news_id, ticker, ledger_seq, p_up_mean, p_up_std, "
                 "novelty_mean, agree, magnitude, stance, pre_move, ref_price, created_at) "
                 "VALUES (%s,%s,%s,%s,0.01,0.6,true,'medium',%s,%s,%s, "
                 "now() - make_interval(hours => %s))",
                 (nid, ticker, e.seq, p, stance, pre, ref, hours_ago))
    return nid


def tradable(conn, *tickers):
    for t in tickers:
        conn.execute("INSERT INTO tradable_assets (ticker, exchange) VALUES (%s, 'NYSE')", (t,))


def test_news_entry_sizing_holdings_and_no_double_orders(conn):
    tradable(conn, "EXMP", "CHSE", "PNNY", "FADE")
    news_signal(conn, "EXMP", p=0.62, ref=100)
    news_signal(conn, "CHSE", p=0.70, ref=100)            # already ran up 10%: skip
    news_signal(conn, "PNNY", p=0.66, ref=3)              # under $5: skip
    news_signal(conn, "FADE", p=0.53)                     # edge too small: skip
    b = FakeBroker()
    mkt = FakeMarket({"EXMP": 101.0, "CHSE": 110.0, "PNNY": 3.0, "FADE": 50.0})
    trader.decide(conn, acct("news", NEWS, b), mkt, TCFG, frozenset(), NOW)
    assert [(s[0], s[1]) for s in b.sent] == [("EXMP", "buy")]
    t, side, qty, lim, coid = b.sent[0]
    assert lim == 101.5 and 0 < qty * lim <= 10_000          # <= one 10% slot
    h = conn.execute("SELECT exit_after, reason FROM holdings WHERE ticker = 'EXMP'").fetchone()
    assert h[0] == date(2026, 10, 15) and "p_up=0.62" in h[1]
    assert conn.execute("SELECT kind FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()[0] == \
        "order"
    trader.decide(conn, acct("news", NEWS, b), mkt, TCFG, frozenset(), NOW)
    assert len(b.sent) == 1                                   # same day rerun: nothing new


def test_fills_are_announced_and_unfilled_buys_cleaned_up(conn):
    tradable(conn, "EXMP", "OTHR")
    news_signal(conn, "EXMP")
    news_signal(conn, "OTHR", p=0.64)
    b = FakeBroker()
    a = acct("news", NEWS, b)
    trader.decide(conn, a, FakeMarket({"EXMP": 100.0, "OTHR": 100.0}), TCFG, frozenset(), NOW)
    assert len(b.sent) == 2
    ex, oth = (s[4] for s in sorted(b.sent))
    b.status[ex] = ("filled", b.sent[[s[4] for s in b.sent].index(ex)][2], 100.2)
    b.status[oth] = ("expired", 0, None)
    b.pos = [Position("EXMP", 99, 100.2, 100.5, 9949.5, 0.003)]
    trader.sync(conn, a, NOW)
    msgs = [r[0] for r in conn.execute("SELECT text FROM notifications WHERE kind = 'trade'")]
    assert len(msgs) == 1 and "BOUGHT" in msgs[0] and "EXMP" in msgs[0]
    assert {r[0] for r in conn.execute("SELECT ticker FROM holdings")} == {"EXMP"}
    assert "EXMP" in reports.positions(conn)
    trader.sync(conn, a, NOW)                                  # no duplicate announcements
    assert conn.execute("SELECT count(*) FROM notifications WHERE kind = 'trade'"
                        ).fetchone()[0] == 1


def test_exits_stop_time_and_contradiction(conn):
    tradable(conn, "STOP", "TIME", "BEAR")
    for t, entry in (("STOP", TODAY), ("TIME", TODAY - timedelta(days=7)), ("BEAR", TODAY)):
        conn.execute("INSERT INTO holdings (account, ticker, entry_day, exit_after, reason) "
                     "VALUES ('news', %s, %s, %s, 'x')", (t, entry, entry + timedelta(days=7)))
    news_signal(conn, "BEAR", p=0.40, stance="agree")
    b = FakeBroker(positions=[Position("STOP", 10, 100, 91, 910, -0.09),
                              Position("TIME", 10, 100, 101, 1010, 0.01),
                              Position("BEAR", 10, 100, 102, 1020, 0.02)])
    trader.decide(conn, acct("news", NEWS, b), FakeMarket(), TCFG, frozenset(), NOW)
    sells = {s[0]: s for s in b.sent if s[1] == "sell"}
    assert set(sells) == {"STOP", "TIME", "BEAR"}
    assert sells["STOP"][3] == round(91 * 0.995, 2)


def test_daily_loss_brake_and_drawdown_halt(conn):
    tradable(conn, "EXMP")
    news_signal(conn, "EXMP")
    b = FakeBroker(equity=96_000, last=100_000)              # down 4% today
    trader.decide(conn, acct("news", NEWS, b), FakeMarket({"EXMP": 100.0}), TCFG, frozenset(),
                  NOW)
    assert b.sent == [] and "no new entries" in conn.execute(
        "SELECT text FROM notifications").fetchone()[0]
    conn.execute("INSERT INTO equity_daily (account, day, equity) VALUES ('news', %s, 100000)",
                 (TODAY - timedelta(days=1),))
    b2 = FakeBroker(equity=84_000)                           # 16% off the peak
    trader.decide(conn, acct("news", NEWS, b2), FakeMarket({"EXMP": 100.0}), TCFG, frozenset(),
                  NOW)
    assert b2.sent == [] and trader.account_halted(conn, "news")
    notifier.set_halt(conn, False)                           # /resume confirm clears it
    assert not trader.account_halted(conn, "news")


def test_risk_off_halves_slots(conn):
    tradable(conn, *[f"S{i}" for i in range(8)])
    for i in range(8):
        news_signal(conn, f"S{i}", p=0.60 + i / 100, ref=None)
    cfg = {**NEWS, "max_positions": 8}
    b = FakeBroker()
    mkt = FakeMarket({f"S{i}": 50.0 for i in range(8)}, spy_up=False)
    trader.decide(conn, acct("news", cfg, b), mkt, TCFG, frozenset(), NOW)
    assert len(b.sent) == 4                                  # SPY in a downtrend: half slots


def test_insider_strategy(conn):
    tradable(conn, "INSD")
    conn.execute("INSERT INTO filings (accession, form_type, cik, ticker, url, raw) "
                 "VALUES ('a1', '4', '1', 'INSD', 'u', '{}')")
    conn.execute("INSERT INTO insider_trades (accession, line_no, ticker, issuer_cik, "
                 "insider_name, officer_title, is_officer, code, acquired_disposed, shares, "
                 "price, value_usd) VALUES ('a1', 1, 'INSD', '0000000001', 'Doe Jane', 'CEO', "
                 "true, 'P', 'A', 5000, 40, 200000)")
    # A closed-end fund with insider buying must be skipped.
    tradable(conn, "FUND")
    conn.execute("INSERT INTO filings (accession, form_type, cik, ticker, url, raw) "
                 "VALUES ('a2', '4', '2', 'FUND', 'u', '{}')")
    conn.execute("INSERT INTO insider_trades (accession, line_no, ticker, issuer_cik, "
                 "insider_name, officer_title, is_officer, code, acquired_disposed, shares, "
                 "price, value_usd) VALUES ('a2', 1, 'FUND', '0000000002', 'Ack Bill', 'CFO', "
                 "true, 'P', 'A', 100000, 40, 9000000)")
    lookups = []

    def sec(cik):
        lookups.append(cik)
        return ("6726", "Closed-end funds") if cik.endswith("2") else ("3674", "Chips")
    b = FakeBroker()
    trader.decide(conn, acct("insider", INSIDER, b), FakeMarket({"INSD": 41.0, "FUND": 40.0}),
                  TCFG, frozenset(), NOW, sec=sec)
    assert [(s[0], s[1]) for s in b.sent] == [("INSD", "buy")]
    assert sorted(lookups) == ["0000000001", "0000000002"]
    trader.decide(conn, acct("insider", INSIDER, b), FakeMarket({"INSD": 41.0, "FUND": 40.0}),
                  TCFG, frozenset(), NOW, sec=sec)
    assert len(lookups) == 2                         # cached: no second SEC lookup
    assert conn.execute("SELECT exit_after FROM holdings").fetchone()[0] == date(2026, 11, 5)


def test_spy_trend_baseline(conn):
    b = FakeBroker()
    trader.decide(conn, acct("baseline", TREND, b), FakeMarket(spy_up=True), TCFG, frozenset(),
                  NOW)
    assert b.sent[0][:2] == ("SPY", "buy") and b.sent[0][2] * 500 <= 95_000 * 1.01
    conn.execute("DELETE FROM prices_daily")
    conn.execute("UPDATE orders SET status = 'filled'")       # the buy went through
    b2 = FakeBroker(positions=[Position("SPY", 190, 500, 480, 91200, -0.04)])
    trader.decide(conn, acct("baseline", TREND, b2), FakeMarket(spy_up=False), TCFG,
                  frozenset(), NOW)
    assert b2.sent[0][:3] == ("SPY", "sell", 190)


def test_flatten_and_snapshot_and_perf(conn):
    b = FakeBroker(positions=[Position("AAA", 5, 10, 11, 55, 0.1)])
    a = acct("news", NEWS, b)
    trader.flatten(conn, [a], NOW, 0.005, pause=0)
    assert b.cancelled == 1 and b.sent[0][:3] == ("AAA", "sell", 5)
    assert get_control(conn, trader.GLOBAL_HALT) == "true"
    # holiday: SPY's last trade isn't today, so no snapshot
    assert not trader.snapshot(conn, a, FakeMarket(trade_day=TODAY - timedelta(days=1)), NOW)
    assert trader.snapshot(conn, a, FakeMarket(), NOW)
    for i, (eq, spy) in enumerate([(100_000, 400), (101_000, 401), (100_500, 403)], start=1):
        conn.execute("INSERT INTO equity_daily (account, day, equity, spy_close) VALUES "
                     "('insider', %s, %s, %s)", (TODAY - timedelta(days=10 - i), eq, spy))
    text = perf.report(conn, 3)
    assert "insider" in text and "vs SPY" in text and "news" in text


def test_bot_trading_commands(conn):
    set_control(conn, "x", "y")
    assert "confirm" in notifier.handle_command(conn, "/flatten", [1])
    assert get_control(conn, "flatten_requested") is None
    notifier.handle_command(conn, "/flatten confirm", [1])
    assert get_control(conn, "flatten_requested") == "true"
    assert "No open positions" in notifier.handle_command(conn, "/positions", [1])
    assert "No end-of-day equity" in notifier.handle_command(conn, "/perf", [1])


def test_accounts_without_keys_are_skipped_with_one_notice(conn, monkeypatch):
    from driftwatch.config import Settings
    s = Settings("", "", "", "", None, {"trading": {"accounts": {"insider": {
        "key_env": "NOPE_KEY", "secret_env": "NOPE_SECRET", "strategy": "insider_follow"}}}},
        frozenset())
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert trader.load_accounts(s, conn) == [] and trader.load_accounts(s, conn) == []
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


# ---------------- regressions for the independent review's findings ----------------

def test_flatten_still_sells_after_a_same_day_exit(conn):
    conn.execute("INSERT INTO holdings (account, ticker, entry_day, exit_after, reason) "
                 "VALUES ('news', 'AAA', %s, %s, 'x')", (TODAY, TODAY + timedelta(days=7)))
    b = FakeBroker(positions=[Position("AAA", 5, 100, 90, 450, -0.10)])
    a = acct("news", NEWS, b)
    trader.decide(conn, a, FakeMarket(), TCFG, frozenset(), NOW, tag="0945")   # stop-loss
    assert [(x[0], x[1]) for x in b.sent] == [("AAA", "sell")]
    assert trader.flatten(conn, [a], NOW + timedelta(minutes=15), 0.005, pause=0)
    assert len(b.sent) == 2 and b.sent[1][4].endswith("flat1015")


def test_resume_after_drawdown_does_not_rehalt(conn):
    conn.execute("INSERT INTO equity_daily (account, day, equity) VALUES ('news', %s, 120000)",
                 (TODAY - timedelta(days=3),))
    b = FakeBroker(equity=100_000)
    trader.decide(conn, acct("news", NEWS, b), FakeMarket(), TCFG, frozenset(), NOW)
    assert trader.account_halted(conn, "news")
    notifier.set_halt(conn, False)
    trader.decide(conn, acct("news", NEWS, b), FakeMarket(), TCFG, frozenset(), NOW)
    assert not trader.account_halted(conn, "news")


def test_drawdown_halt_keeps_stop_losses(conn):
    conn.execute("INSERT INTO equity_daily (account, day, equity) VALUES ('news', %s, 120000)",
                 (TODAY - timedelta(days=3),))
    conn.execute("INSERT INTO holdings (account, ticker, entry_day, exit_after, reason) "
                 "VALUES ('news', 'DOWN', %s, %s, 'x')", (TODAY, TODAY + timedelta(days=7)))
    b = FakeBroker(equity=100_000, positions=[Position("DOWN", 10, 100, 85, 850, -0.15)])
    trader.decide(conn, acct("news", NEWS, b), FakeMarket(), TCFG, frozenset(), NOW)
    assert [(x[0], x[1]) for x in b.sent] == [("DOWN", "sell")]


def test_fill_between_calls_keeps_exit_rules(conn):
    tradable(conn, "EXMP")
    news_signal(conn, "EXMP")
    b = FakeBroker()
    a = acct("news", NEWS, b)
    trader.decide(conn, a, FakeMarket({"EXMP": 100.0}), TCFG, frozenset(), NOW)
    # Broker still says 'new' when asked, but the position already exists.
    b.pos = [Position("EXMP", 99, 100.2, 100.3, 9929.7, 0.001)]
    trader.sync(conn, a, NOW)
    assert conn.execute("SELECT count(*) FROM holdings").fetchone()[0] == 1


def test_timeout_order_is_adopted_with_exit_rules(conn, monkeypatch):
    import httpx
    tradable(conn, "EXMP")
    news_signal(conn, "EXMP")
    b = FakeBroker()
    real = b.submit

    def flaky(*args):
        real(*args)                                   # Alpaca got it...
        raise httpx.ReadTimeout("no answer")          # ...but we never heard back
    b.submit = flaky
    a = acct("news", NEWS, b)
    trader.decide(conn, a, FakeMarket({"EXMP": 100.0}), TCFG, frozenset(), NOW)
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "unknown"
    assert conn.execute("SELECT count(*) FROM holdings").fetchone()[0] == 0
    coid = b.sent[0][4]
    b.status[coid] = ("filled", b.sent[0][2], 100.4)
    b.pos = [Position("EXMP", b.sent[0][2], 100.4, 100.5, 9000, 0.001)]
    trader.sync(conn, a, NOW)
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "filled"
    h = conn.execute("SELECT exit_after FROM holdings WHERE ticker = 'EXMP'").fetchone()
    assert h is not None                              # exit rules restored


def test_order_that_never_arrived_is_marked_lost(conn):
    b = FakeBroker()
    conn.execute("INSERT INTO orders (client_order_id, account, ticker, side, qty, reason, "
                 "status, submitted_at) VALUES ('ghost', 'news', 'ZZZ', 'buy', 1, 'x', "
                 "'unknown', now() - interval '20 minutes')")
    trader.sync(conn, acct("news", NEWS, b), datetime.now(UTC))
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "lost"


def test_later_slot_can_replace_an_unfilled_exit(conn):
    conn.execute("INSERT INTO holdings (account, ticker, entry_day, exit_after, reason) "
                 "VALUES ('news', 'FALL', %s, %s, 'x')", (TODAY, TODAY + timedelta(days=7)))
    b = FakeBroker(positions=[Position("FALL", 10, 100, 90, 900, -0.10)])
    a = acct("news", NEWS, b)
    trader.decide(conn, a, FakeMarket(), TCFG, frozenset(), NOW, tag="0945")
    trader.decide(conn, a, FakeMarket(), TCFG, frozenset(), NOW, tag="0945")   # same slot
    trader.decide(conn, a, FakeMarket(), TCFG, frozenset(), NOW, tag="1230")   # next slot
    assert [x[4][-4:] for x in b.sent] == ["0945", "1230"]


def test_baseline_survives_missing_spy_quote(conn):
    class NoQuote(FakeMarket):
        def snapshots(self, tickers):
            return {}
    b = FakeBroker()
    trader.decide(conn, acct("baseline", TREND, b), NoQuote(spy_up=True), TCFG, frozenset(),
                  NOW)
    assert b.sent == []


def test_kill_requests_order_cancellation(conn):
    notifier.set_halt(conn, True)
    assert get_control(conn, "kill_cancel_done") == "false"


def test_trend_account_never_stacks_a_second_spy_buy(conn):
    b = FakeBroker()
    a = acct("baseline", TREND, b)
    trader.decide(conn, a, FakeMarket(spy_up=True), TCFG, frozenset(), NOW, tag="0945")
    trader.decide(conn, a, FakeMarket(spy_up=True), TCFG, frozenset(), NOW, tag="1230")
    assert len(b.sent) == 1                    # first buy still working: no second one


def test_flatten_retry_keeps_good_sells_working(conn):
    b = FakeBroker(positions=[Position("AAA", 5, 10, 11, 55, 0.1),
                              Position("BBB", 5, 10, 11, 55, 0.1)])
    real = b.submit

    def fail_bbb(t, side, qty, lim, coid):
        if t == "BBB":
            raise BrokerError(403, "insufficient qty")
        return real(t, side, qty, lim, coid)
    b.submit = fail_bbb
    a = acct("news", NEWS, b)
    assert not trader.flatten(conn, [a], NOW, 0.005, pause=0)
    assert b.cancelled == 1 and [x[0] for x in b.sent] == ["AAA"]
    b.submit = real
    assert trader.flatten(conn, [a], NOW + timedelta(minutes=1), 0.005, pause=0)
    assert b.cancelled == 1                     # retry didn't cancel the working AAA sell
    assert sorted(x[0] for x in b.sent) == ["AAA", "BBB"]
    assert get_control(conn, "flatten_requested") == "false"


def test_one_failing_account_does_not_block_others(conn, monkeypatch):
    from driftwatch.config import Settings

    class Broken(FakeBroker):
        def cancel_open(self):
            raise BrokerError(503, "down")

    good, bad = FakeBroker(), Broken()
    accts = [acct("news", NEWS, bad), acct("insider", INSIDER, good)]
    notifier.set_halt(conn, True)               # /kill: loop must cancel orders everywhere
    s = Settings("", "", "", "", None, {"trading": {**TCFG, "run_times": ["09:45"],
                                                    "snapshot_after": "23:59"}}, frozenset())
    calls = {"n": 0}

    def stop(_):
        calls["n"] += 1
        raise KeyboardInterrupt
    monkeypatch.setattr(trader.time, "sleep", stop)

    class Clock:
        is_open, next_close = False, NOW
    good.clock = bad.clock = lambda: Clock()
    try:
        trader.run(s, conn, accts, FakeMarket())
    except KeyboardInterrupt:
        pass
    assert good.cancelled == 1                  # the healthy account still got cancelled
    assert get_control(conn, "kill_cancel_done") == "false"   # broken one: retried later


def test_flatten_with_one_broken_account_still_sells_and_is_capped(conn):
    class Broken(FakeBroker):
        def cancel_open(self):
            raise BrokerError(500, "down")

        def positions(self):
            raise BrokerError(500, "down")

    good = FakeBroker(positions=[Position("AAA", 5, 10, 11, 55, 0.1)])
    accts = [acct("news", NEWS, Broken()), acct("insider", INSIDER, good)]
    for i in range(5):
        trader.flatten(conn, accts, NOW + timedelta(minutes=i), 0.005, pause=0)
    assert [x[0] for x in good.sent] == ["AAA"]           # healthy account sold at once
    assert get_control(conn, "flatten_requested") == "false"    # gave up after 5 tries
    assert "could not be placed" in conn.execute(
        "SELECT text FROM notifications ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_first_snapshot_records_the_starting_point(conn):
    b = FakeBroker(equity=100_197, last=100_000)
    a = acct("insider", INSIDER, b)
    assert trader.snapshot(conn, a, FakeMarket(), NOW)
    rows = conn.execute("SELECT day, equity FROM equity_daily WHERE account = 'insider' "
                        "ORDER BY day").fetchall()
    assert rows == [(TODAY - timedelta(days=1), 100_000), (TODAY, 100_197)]
    assert trader.prev_weekday(date(2026, 10, 12)) == date(2026, 10, 9)   # Mon -> Fri


def test_start_rows_repairs_accounts_missing_day_one(conn):
    conn.execute("INSERT INTO prices_daily (ticker, day, open, close, volume) VALUES "
                 "('SPY', '2026-10-07', 770, 772.5, 1), ('SPY', '2026-10-08', 776, 771, 1)")
    conn.execute("INSERT INTO equity_daily (account, day, equity, spy_close) VALUES "
                 "('news', '2026-10-08', 99972, 771)")
    assert trader.start_rows(conn, 100_000) == [("news", date(2026, 10, 7), 100_000, 772.5)]
    assert trader.start_rows(conn, 100_000) == []                         # idempotent
