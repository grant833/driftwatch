"""Trader service: turns signals into paper orders, manages exits and risk, and records
everything (orders in the ledger, fills to Telegram, end-of-day equity for /perf).

Order of safety checks on every decision run, per account:
  global /kill  ->  account drawdown halt  ->  broker says account blocked
  ->  exits (stop-loss, time, contradiction)  ->  daily-loss brake on new entries
  ->  risk-off (SPY under its 200-day average) halves the slots  ->  entries
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from html import escape

from . import ledger
from .alerts import alert, notify
from .broker import FINAL, AlpacaBroker, Broker, BrokerError
from .config import Settings
from .db import get_control, set_control
from .guards import can_trade
from .prices import ET, completed_cutoff
from .strategies import (
    add_trading_days,
    contradicted,
    daily_vol,
    drawdown,
    insider_candidates,
    limit_price,
    news_candidates,
    operating_company,
    plan_exits,
    position_dollars,
    shares_for,
    trend_on,
)

log = logging.getLogger(__name__)
GLOBAL_HALT = "trading_halted"
OPEN_EXCLUDE = sorted(FINAL | {"error", "lost"})   # statuses meaning "no longer working"
LOST_AFTER = timedelta(minutes=10)


@dataclass
class Acct:
    name: str
    cfg: dict
    broker: Broker


def account_halted(conn, name: str) -> bool:
    return (get_control(conn, GLOBAL_HALT) == "true"
            or get_control(conn, f"halt:{name}") == "true")


def client_id(account: str, ticker: str, side: str, day: date, tag: str = "") -> str:
    """Deterministic per account/ticker/side/day/run slot: a restart can't double-submit,
    but a later run slot (or a flatten) can still replace an order that didn't fill."""
    return f"dw-{account}-{ticker}-{side}-{day:%Y%m%d}" + (f"-{tag}" if tag else "")


def parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


# ---------------- price helpers ----------------

def store_bars(conn, rows) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO prices_daily (ticker, day, open, close, volume) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (ticker, day) DO UPDATE SET open = EXCLUDED.open, "
            "close = EXCLUDED.close, volume = EXCLUDED.volume", rows)


def closes(conn, ticker: str, n: int) -> list[float]:
    rows = conn.execute("SELECT close FROM prices_daily WHERE ticker = %s ORDER BY day DESC "
                        "LIMIT %s", (ticker, n)).fetchall()
    return [r[0] for r in reversed(rows)]


def ensure_history(conn, prices, tickers: list[str], days: int, now: datetime) -> None:
    """Make sure each ticker has ~`days` calendar days of bars up to the last close."""
    cutoff = completed_cutoff(now)
    need = []
    for t in tickers:
        row = conn.execute("SELECT min(day), max(day) FROM prices_daily WHERE ticker = %s",
                           (t,)).fetchone()
        if not row[0] or row[1] < cutoff - timedelta(days=4) or row[0] > cutoff - \
                timedelta(days=int(days * 0.9)):
            need.append(t)
    if need:
        store_bars(conn, prices.daily_bars(need, cutoff - timedelta(days=days), cutoff))


def spy_trend_state(conn, prices, sma_days: int, now: datetime) -> bool | None:
    ensure_history(conn, prices, ["SPY"], int(sma_days * 1.6), now)
    return trend_on(closes(conn, "SPY", sma_days + 5), sma_days)


# ---------------- orders ----------------

def submit(conn, a: Acct, ticker: str, side: str, qty: int, price: float, reason: str,
           today: date, slip: float, news_id: int | None = None,
           seq: int | None = None, tag: str = "") -> bool:
    coid = client_id(a.name, ticker, side, today, tag)
    if conn.execute("SELECT 1 FROM orders WHERE client_order_id = %s", (coid,)).fetchone():
        return False                      # already placed in this slot; never double-submit
    lim = limit_price(price, side, slip)
    entry = ledger.append(conn, "order", {
        "account": a.name, "ticker": ticker, "side": side, "qty": qty, "limit": lim,
        "reason": reason, "news_id": news_id, "prediction_seq": seq,
        "client_order_id": coid})
    conn.execute(
        "INSERT INTO orders (client_order_id, account, ticker, side, qty, limit_price, reason, "
        "ref_news_id, ledger_seq, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
        (coid, a.name, ticker, side, qty, lim, reason, news_id, entry.seq))
    try:
        resp = a.broker.submit(ticker, side, qty, lim, coid)
    except BrokerError as exc:
        conn.execute("UPDATE orders SET status = 'error', updated_at = now() "
                     "WHERE client_order_id = %s", (coid,))
        notify(conn, "alert", f"⚠ [{a.name}] {side} {ticker} rejected: {escape(str(exc)[:200])}")
        return False
    except Exception as exc:
        # Timeout or dropped connection: Alpaca may or may not have the order. Leave it
        # 'unknown'; sync looks it up by our id and either adopts it or marks it lost.
        conn.execute("UPDATE orders SET status = 'unknown', updated_at = now() "
                     "WHERE client_order_id = %s", (coid,))
        log.warning("[%s] %s %s: no answer from broker (%s); will reconcile", a.name, side,
                    ticker, exc)
        return False
    conn.execute("UPDATE orders SET broker_order_id = %s, status = %s, updated_at = now() "
                 "WHERE client_order_id = %s", (resp.get("id"), resp.get("status", "new"), coid))
    log.info("[%s] %s %d %s @<=%.2f (%s)", a.name, side.upper(), qty, ticker, lim, reason)
    return True


def fill_message(conn, a: Acct, o: dict) -> str:
    qty, px = o["filled_qty"], o["filled_avg_price"]
    if o["side"] == "buy":
        return (f"🟢 <b>[{a.name}] BOUGHT {qty:g} {escape(o['ticker'])}</b> @ ${px:.2f}\n"
                f"<i>{escape(o['reason'][:160])}</i>")
    row = conn.execute(
        "SELECT filled_avg_price FROM orders WHERE account = %s AND ticker = %s AND side = 'buy' "
        "AND filled_qty > 0 ORDER BY submitted_at DESC LIMIT 1", (a.name, o["ticker"])).fetchone()
    pl = f" ({px / row[0] - 1:+.1%})" if row and row[0] else ""
    return (f"🔴 <b>[{a.name}] SOLD {qty:g} {escape(o['ticker'])}</b> @ ${px:.2f}{pl}\n"
            f"<i>{escape(o['reason'][:160])}</i>")


def sync(conn, a: Acct, now: datetime) -> None:
    """Refresh order statuses (first) and positions (second), announce fills, and keep
    the holdings notes consistent. Orders are read before positions so a buy that fills
    in between is seen as still open, never as 'gone'."""
    open_rows = conn.execute(
        "SELECT client_order_id, submitted_at FROM orders WHERE account = %s "
        "AND status <> ALL(%s)", (a.name, OPEN_EXCLUDE)).fetchall()
    for coid, submitted in open_rows:
        o = a.broker.order(coid)
        if o is None:
            if now - submitted > LOST_AFTER:
                conn.execute("UPDATE orders SET status = 'lost', updated_at = now() "
                             "WHERE client_order_id = %s", (coid,))
                log.warning("[%s] order %s never reached the broker", a.name, coid)
            continue
        fq = float(o.get("filled_qty") or 0)
        fp = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        conn.execute("UPDATE orders SET status = %s, filled_qty = %s, filled_avg_price = %s, "
                     "broker_order_id = %s, updated_at = now() WHERE client_order_id = %s",
                     (o.get("status"), fq, fp, o.get("id"), coid))
    positions = a.broker.positions()
    with conn.transaction():
        conn.execute("DELETE FROM positions_live WHERE account = %s", (a.name,))
        for p in positions:
            conn.execute(
                "INSERT INTO positions_live (account, ticker, qty, avg_entry_price, "
                "current_price, market_value, unrealized_plpc) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (a.name, p.ticker, p.qty, p.avg_entry_price, p.current_price, p.market_value,
                 p.unrealized_plpc))
    held = {p.ticker for p in positions}
    done = conn.execute(
        "SELECT client_order_id, ticker, side, reason, filled_qty, filled_avg_price, status "
        "FROM orders WHERE account = %s AND NOT notified AND status = ANY(%s)",
        (a.name, list(FINAL))).fetchall()
    for coid, tkr, side, reason, fq, fp, status in done:
        o = {"ticker": tkr, "side": side, "reason": reason, "filled_qty": fq or 0,
             "filled_avg_price": fp}
        if (fq or 0) > 0 and fp:
            notify(conn, "trade", fill_message(conn, a, o))
        elif status == "rejected":
            notify(conn, "alert", f"⚠ [{a.name}] order for {escape(tkr)} was rejected")
        conn.execute("UPDATE orders SET notified = true WHERE client_order_id = %s", (coid,))
    # Any position we bought must carry exit rules, even if its holdings note was lost
    # (e.g. the order went through during a broker timeout).
    for tkr, reason, news_id, submitted in conn.execute(
            "SELECT DISTINCT ON (ticker) ticker, reason, ref_news_id, submitted_at FROM orders "
            "WHERE account = %s AND side = 'buy' AND filled_qty > 0 "
            "AND submitted_at > now() - interval '45 days' ORDER BY ticker, submitted_at DESC",
            (a.name,)).fetchall():
        if tkr in held:
            entry = submitted.astimezone(ET).date()
            hold = a.cfg.get("hold_days")
            exit_after = add_trading_days(entry, hold) if hold else date(2999, 12, 31)
            conn.execute("INSERT INTO holdings (account, ticker, entry_day, exit_after, reason, "
                         "ref_news_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (a.name, tkr, entry, exit_after, reason, news_id))
    # Drop notes for positions that are gone (sold, or a buy that never filled).
    pending = {r[0] for r in conn.execute(
        "SELECT ticker FROM orders WHERE account = %s AND side = 'buy' AND status <> ALL(%s)",
        (a.name, OPEN_EXCLUDE))}
    for (tkr,) in conn.execute("SELECT ticker FROM holdings WHERE account = %s",
                               (a.name,)).fetchall():
        if tkr not in held and tkr not in pending:
            conn.execute("DELETE FROM holdings WHERE account = %s AND ticker = %s", (a.name, tkr))


# ---------------- decisions ----------------

def universe_ok(conn, ticker: str, blocklist: frozenset[str]) -> bool:
    funds = conn.execute("SELECT 1 FROM fund_tickers WHERE ticker = %s", (ticker,)).fetchone()
    tradable_n = conn.execute("SELECT count(*) FROM tradable_assets").fetchone()[0]
    tradable = conn.execute("SELECT 1 FROM tradable_assets WHERE ticker = %s",
                            (ticker,)).fetchone()
    return (can_trade(ticker, blocklist, frozenset({ticker}) if funds else frozenset())[0]
            and (tradable_n == 0 or tradable is not None))


def decide(conn, a: Acct, prices, tcfg: dict, blocklist: frozenset[str], now: datetime,
           tag: str = "", entries: bool = True, sec=None) -> None:
    """One decision run. Exits (stop-loss, time, contradiction) always run; new entries
    only when `entries` is true and the account isn't in a drawdown or daily-loss halt."""
    today = now.astimezone(ET).date()
    acct = a.broker.account()
    if acct.blocked:
        log.warning("[%s] broker reports the account is blocked", a.name)
        return
    # Peak equity since the last /resume, so a reviewed drawdown doesn't re-halt at once.
    since = get_control(conn, "peak_reset_day") or "1900-01-01"
    peak = max(acct.equity, conn.execute(
        "SELECT coalesce(max(equity), 0) FROM equity_daily WHERE account = %s AND day >= %s",
        (a.name, since)).fetchone()[0])
    dd = drawdown(acct.equity, peak)
    if dd <= -tcfg["halt_drawdown"]:
        entries = False
        if get_control(conn, f"halt:{a.name}") != "true":
            set_control(conn, f"halt:{a.name}", "true")
            alert(None, f"[{a.name}] drawdown {dd:.1%} from peak: no new entries until you "
                        f"review and send /resume confirm (stop-losses stay active).", conn=conn)
    positions = a.broker.positions()
    slip = tcfg["limit_slippage"]
    if a.cfg["strategy"] == "spy_trend":
        return decide_trend(conn, a, prices, tcfg, acct, positions, today, now, slip, tag,
                            entries)

    holdings = {r[0]: {"entry_day": r[1], "exit_after": r[2]} for r in conn.execute(
        "SELECT ticker, entry_day, exit_after FROM holdings WHERE account = %s", (a.name,))}
    exits = plan_exits(positions, holdings, today, a.cfg["stop_loss"],
                       contradicted(conn, a.name) if a.cfg["strategy"] == "news_drift" else set())
    by_tkr = {p.ticker: p for p in positions}
    for e in exits:
        p = by_tkr[e.ticker]
        submit(conn, a, e.ticker, "sell", int(p.qty), p.current_price, e.reason, today, slip,
               tag=tag)
    exiting = {e.ticker for e in exits}
    if not entries:
        return

    if acct.last_equity > 0 and acct.equity / acct.last_equity - 1 <= -tcfg["halt_daily_loss"]:
        key = f"dailyloss:{a.name}:{today}"
        if not get_control(conn, key):
            set_control(conn, key, "1")
            notify(conn, "alert", f"🟠 [{a.name}] down {acct.equity / acct.last_equity - 1:.1%} "
                                  f"today: no new entries until tomorrow")
        return

    risk_off = spy_trend_state(conn, prices, 200, now) is False
    slots = int(a.cfg["max_positions"] * (tcfg["risk_off_slots"] if risk_off else 1))
    pending_buys = conn.execute(
        "SELECT ticker, qty * limit_price FROM orders WHERE account = %s AND side = 'buy' "
        "AND status <> ALL(%s)", (a.name, OPEN_EXCLUDE)).fetchall()
    held = {p.ticker for p in positions}
    occupied = len(held - exiting) + len({t for t, _ in pending_buys} - held)
    if occupied >= slots:
        return
    invested = sum(p.market_value for p in positions) + sum(v or 0 for _, v in pending_buys)
    room = min(acct.equity * tcfg["max_invested"] - invested, acct.cash)

    if a.cfg["strategy"] == "news_drift":
        cands = news_candidates(conn, a.cfg, a.name)
    else:
        cands = [c for c in insider_candidates(conn, a.cfg, a.name)
                 if operating_company(conn, c.cik, sec)]
    cands = [c for c in cands if c.ticker not in held | {t for t, _ in pending_buys}
             and universe_ok(conn, c.ticker, blocklist)][:3 * slots]
    if not cands:
        return
    snaps = prices.snapshots([c.ticker for c in cands])
    ensure_history(conn, prices, [c.ticker for c in cands if c.ticker in snaps], 45, now)
    for c in cands:
        if occupied >= slots or room <= 0:
            break
        snap = snaps.get(c.ticker)
        if not snap or snap["price"] < tcfg["min_price"]:
            continue
        price = snap["price"]
        if c.ref_price and price > c.ref_price * (1 + a.cfg["max_chase"]):
            log.info("[%s] skip %s: already up %.1f%% since the signal", a.name, c.ticker,
                     (price / c.ref_price - 1) * 100)
            continue
        dollars = min(position_dollars(acct.equity, daily_vol(closes(conn, c.ticker, 21)),
                                       a.cfg, tcfg), room)
        qty = shares_for(dollars, limit_price(price, "buy", slip))
        if qty < 1:
            continue
        if submit(conn, a, c.ticker, "buy", qty, price, c.reason, today, slip, c.ref_news_id,
                  c.ledger_seq, tag):
            conn.execute(
                "INSERT INTO holdings (account, ticker, entry_day, exit_after, reason, "
                "ref_news_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (account, ticker) DO NOTHING",
                (a.name, c.ticker, today, add_trading_days(today, a.cfg["hold_days"]), c.reason,
                 c.ref_news_id))
            occupied += 1
            room -= qty * price


def decide_trend(conn, a: Acct, prices, tcfg, acct, positions, today, now, slip, tag="",
                 entries=True) -> None:
    on = spy_trend_state(conn, prices, a.cfg["sma_days"], now)
    if on is None:
        log.warning("[%s] not enough SPY history for the trend signal yet", a.name)
        return
    spy = next((p for p in positions if p.ticker == "SPY"), None)
    working = {r[0] for r in conn.execute(
        "SELECT side FROM orders WHERE account = %s AND ticker = 'SPY' AND status <> ALL(%s)",
        (a.name, OPEN_EXCLUDE))}
    if working:                  # an earlier order may still fill; never stack another
        log.info("[%s] SPY order still working (%s); waiting", a.name, ",".join(working))
        return
    if on and spy is None and entries:
        snap = prices.snapshots(["SPY"]).get("SPY")
        if not snap:
            log.warning("[%s] no SPY quote right now; will try next run", a.name)
            return
        qty = shares_for(min(acct.equity * tcfg["max_invested"], acct.cash),
                         limit_price(snap["price"], "buy", slip))
        if qty >= 1 and submit(conn, a, "SPY", "buy", qty, snap["price"],
                               "SPY above its 200-day average: trend on", today, slip, tag=tag):
            conn.execute("INSERT INTO holdings (account, ticker, entry_day, exit_after, reason) "
                         "VALUES (%s,'SPY',%s,'2999-12-31','trend') ON CONFLICT DO NOTHING",
                         (a.name, today))
    elif on is False and spy is not None:
        submit(conn, a, "SPY", "sell", int(spy.qty), spy.current_price,
               "SPY fell below its 200-day average: trend off", today, slip, tag=tag)


def flatten(conn, accts: list[Acct], now: datetime, slip: float, pause: float = 3.0) -> bool:
    """Sell everything everywhere, then stay halted. Open orders are cancelled first (and
    given a moment to clear) so shares aren't still reserved. Each attempt uses its own
    order ids; if any sell can't be placed, the request stays on and retries next minute,
    up to 5 attempts."""
    set_control(conn, GLOBAL_HALT, "true")
    set_control(conn, "kill_cancel_done", "true")
    tries = int(get_control(conn, "flatten_attempts") or 0) + 1
    set_control(conn, "flatten_attempts", str(tries))   # count first, so retries are capped
    ok = True
    if tries == 1:               # first attempt only: clear everything that was working
        for a in accts:
            try:
                a.broker.cancel_open()
            except Exception as exc:
                ok = False
                log.warning("[%s] flatten: cancel failed: %s", a.name, exc)
        if pause:
            time.sleep(pause)
    today = now.astimezone(ET).date()
    tag = f"flat{now.astimezone(ET):%H%M}"
    for a in accts:              # one account's broker trouble never stops the others
        try:
            selling = {r[0] for r in conn.execute(
                "SELECT ticker FROM orders WHERE account = %s AND side = 'sell' "
                "AND reason = 'manual /flatten' AND status <> ALL(%s)",
                (a.name, OPEN_EXCLUDE))}
            for p in a.broker.positions():
                if p.ticker in selling:
                    continue     # a flatten sell from an earlier attempt is still working
                ok &= submit(conn, a, p.ticker, "sell", int(p.qty), p.current_price,
                             "manual /flatten", today, slip * 2, tag=tag)
        except Exception as exc:
            ok = False
            log.warning("[%s] flatten: sells failed: %s", a.name, exc)
    if ok or tries >= 5:
        set_control(conn, "flatten_requested", "false")
        set_control(conn, "flatten_attempts", "0")
        notify(conn, "alert", "🧯 Flatten sent: selling every position in every account. "
                              "Trading is HALTED until /resume confirm." if ok else
               "🧯 Flatten: some sells could not be placed after 5 tries. Check the Alpaca "
               "dashboard. Trading is HALTED.")
    return ok


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def start_rows(conn, equity: float) -> list[tuple]:
    """One-time repair for accounts snapshotted before start rows existed: add a row on the
    trading day before each account's first snapshot, with the starting equity and SPY's
    close that day (from the scorer's price table)."""
    added = []
    for acct, first in conn.execute(
            "SELECT account, min(day) FROM equity_daily GROUP BY 1").fetchall():
        spy = conn.execute("SELECT day, close FROM prices_daily WHERE ticker = 'SPY' "
                           "AND day < %s ORDER BY day DESC LIMIT 1", (first,)).fetchone()
        if not spy:
            continue
        cur = conn.execute("INSERT INTO equity_daily (account, day, equity, positions, "
                           "spy_close) VALUES (%s,%s,%s,0,%s) ON CONFLICT DO NOTHING",
                           (acct, spy[0], equity, spy[1]))
        if cur.rowcount:
            added.append((acct, spy[0], equity, spy[1]))
    return added


def snapshot(conn, a: Acct, prices, now: datetime) -> bool:
    """Record today's closing equity once, only on actual trading days."""
    today = now.astimezone(ET).date()
    if conn.execute("SELECT 1 FROM equity_daily WHERE account = %s AND day = %s",
                    (a.name, today)).fetchone():
        return False
    spy = prices.snapshots(["SPY"]).get("SPY")
    if not spy or not spy.get("trade_time") or datetime.fromisoformat(
            spy["trade_time"].replace("Z", "+00:00")).astimezone(ET).date() != today:
        return False                     # market holiday: no new trades today
    acct = a.broker.account()
    n = len(a.broker.positions())
    if not conn.execute("SELECT 1 FROM equity_daily WHERE account = %s", (a.name,)).fetchone():
        # First snapshot ever: also record where the account STARTED (yesterday's close
        # equity and SPY's prior close), or day one's gain or loss would never be counted.
        conn.execute("INSERT INTO equity_daily (account, day, equity, positions, spy_close) "
                     "VALUES (%s,%s,%s,0,%s) ON CONFLICT DO NOTHING",
                     (a.name, prev_weekday(today), acct.last_equity, spy["prev_close"]))
    conn.execute("INSERT INTO equity_daily (account, day, equity, cash, positions, spy_close) "
                 "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (a.name, today, acct.equity, acct.cash, n, spy["price"]))
    return True


# ---------------- service ----------------

def sec_lookup(user_agent: str):
    """(cik) -> (sic, description) from SEC's company submissions file."""
    import httpx
    client = httpx.Client(headers={"User-Agent": user_agent}, timeout=20)

    def fetch(cik: str) -> tuple[str, str]:
        r = client.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        r.raise_for_status()
        d = r.json()
        return str(d.get("sic") or ""), str(d.get("sicDescription") or "")
    return fetch


def load_accounts(s: Settings, conn) -> list[Acct]:
    tcfg = s.raw["trading"]
    url = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets")
    out = []
    for name, cfg in tcfg["accounts"].items():
        key, secret = os.getenv(cfg["key_env"], ""), os.getenv(cfg["secret_env"], "")
        if not (key and secret):
            log.warning("account %s: %s not set; skipping", name, cfg["key_env"])
            if not get_control(conn, f"warned_keys:{name}"):
                set_control(conn, f"warned_keys:{name}", "1")
                notify(conn, "alert", f"ℹ Paper account <b>{name}</b> is not set up yet: add "
                                      f"{cfg['key_env']} and {cfg['secret_env']} to .env")
            continue
        out.append(Acct(name, cfg, AlpacaBroker(key, secret, url,
                                                tcfg.get("allow_live") is True)))
    return out


def due_slot(now: datetime, run_times: list[str], done: str | None) -> str | None:
    """Latest scheduled time that has passed today and hasn't run (missed slots collapse)."""
    et = now.astimezone(ET)
    passed = [t for t in run_times if et.time() >= parse_hhmm(t)]
    if not passed:
        return None
    slot = f"{et.date()}T{passed[-1]}"
    return slot if slot != done else None


def run(s: Settings, conn, accts: list[Acct] | None = None, prices=None) -> None:
    tcfg = s.raw["trading"]
    accts = accts if accts is not None else load_accounts(s, conn)
    if not accts:
        log.error("no trading accounts configured; trader idle")
    if prices is None:
        from .prices import AlpacaPrices
        prices = AlpacaPrices(s.alpaca_key, s.alpaca_secret)
    clock, clock_at, last_sync = None, 0.0, 0.0
    sec = sec_lookup(s.sec_user_agent)

    def each(label, fn) -> bool:
        """Run fn(account) for every account; one account's API trouble never blocks
        the others or the safety actions. Returns True only if all succeeded."""
        ok = True
        for a in accts:
            try:
                fn(a)
            except Exception as exc:
                ok = False
                log.exception("[%s] %s failed", a.name, label)
                alert(None, f"[{a.name}] {label} failed: {str(exc)[:200]}", conn=conn)
        return ok

    while True:
        try:
            now = datetime.now(UTC)
            if accts and (clock is None or time.time() - clock_at > 300):
                clock, clock_at = accts[0].broker.clock(), time.time()
            is_open = bool(clock and clock.is_open and now < clock.next_close)
            halted = get_control(conn, GLOBAL_HALT) == "true"
            # Safety actions come first, before any routine work that could fail.
            if halted and get_control(conn, "kill_cancel_done") != "true":
                if each("cancel orders", lambda a: a.broker.cancel_open()):
                    set_control(conn, "kill_cancel_done", "true")   # else retry next minute
                    notify(conn, "alert", "🛑 Trading halted: open orders cancelled.")
            if is_open and get_control(conn, "flatten_requested") == "true":
                try:
                    flatten(conn, accts, now, tcfg["limit_slippage"])
                except Exception as exc:
                    log.exception("flatten failed")
                    alert(None, f"flatten attempt failed, retrying: {exc}", conn=conn)
                halted = True
            if is_open and time.time() - last_sync > 300:
                last_sync = time.time()
                each("sync", lambda a, now=now: sync(conn, a, now))
            slot = due_slot(now, tcfg["run_times"], get_control(conn, "trader_slot"))
            if is_open and slot and tcfg.get("enabled", True) is not False and not halted:
                set_control(conn, "trader_slot", slot)
                tag = slot[-5:].replace(":", "")
                each("cancel stale orders", lambda a: a.broker.cancel_open())
                time.sleep(3)
                each("sync", lambda a, now=now: sync(conn, a, now))
                each("decision run", lambda a, now=now, tag=tag: decide(
                    conn, a, prices, tcfg, s.blocklist, now, tag,
                    entries=not account_halted(conn, a.name), sec=sec))
                each("sync", lambda a, now=now: sync(conn, a, now))
                last_sync = time.time()
            et = now.astimezone(ET)
            if (et.weekday() < 5 and et.time() >= parse_hhmm(tcfg["snapshot_after"])
                    and get_control(conn, "snapshot_day") != et.date().isoformat()):
                each("end-of-day sync", lambda a, now=now: sync(conn, a, now))
                each("equity snapshot", lambda a, now=now: snapshot(conn, a, prices, now))
                set_control(conn, "snapshot_day", et.date().isoformat())
        except Exception as exc:
            log.exception("trader cycle failed: %s", exc)
        time.sleep(60)
