"""Scorekeeper: grades every prediction against what the stock actually did.

Entry rules are deliberately conservative and point-in-time honest, using only
prices we could actually have traded at after the prediction was made:

  * made before 9:30 ET on a trading day -> enter at that day's OPEN
  * made during market hours             -> enter at that day's CLOSE
  * made after the close or on a holiday -> enter at the next trading day's OPEN

An h-session horizon exits at the close h sessions after entry (an open entry's
first session is the entry day itself). The score is the stock's return minus
SPY's return over exactly the same entry and exit points.
"""
from __future__ import annotations

import bisect
import logging
import math
import statistics
import time
from datetime import date, datetime, timedelta

from .alerts import alert
from .config import Settings
from .db import get_control, set_control
from .prices import ET, MARKET_CLOSE, MARKET_OPEN, PriceSource, completed_cutoff

log = logging.getLogger(__name__)
BENCHMARK = "SPY"


# ---------------- pure logic (unit tested) ----------------

def entry_point(made_at: datetime, days: list[date]) -> tuple[int, str] | None:
    """Index into the sorted trading-day list, and 'open' or 'close'. None if not yet known."""
    et = made_at.astimezone(ET)
    d, t = et.date(), et.time()
    i = bisect.bisect_left(days, d)
    on_trading_day = i < len(days) and days[i] == d
    if on_trading_day and t < MARKET_OPEN:
        return i, "open"
    if on_trading_day and t < MARKET_CLOSE:
        return i, "close"
    nxt = i + 1 if on_trading_day else i
    return (nxt, "open") if nxt < len(days) else None


def exit_index(entry_idx: int, kind: str, horizon: int) -> int:
    return entry_idx + horizon - (1 if kind == "open" else 0)


def score_one(made_at: datetime, ticker: str, horizon: int, days: list[date],
              px: dict[tuple[str, date], tuple[float, float]]) -> dict | None:
    """px maps (ticker, day) -> (open, close). Returns an outcome row or None."""
    ep = entry_point(made_at, days)
    if ep is None:
        return None
    ei, kind = ep
    xi = exit_index(ei, kind, horizon)
    if xi >= len(days):
        return None
    ed, xd = days[ei], days[xi]
    s_in, s_out = px.get((ticker, ed)), px.get((ticker, xd))
    b_in, b_out = px.get((BENCHMARK, ed)), px.get((BENCHMARK, xd))
    if not (s_in and s_out and b_in and b_out):
        return None
    pick = 0 if kind == "open" else 1
    entry_px, bench_in = s_in[pick], b_in[pick]
    if entry_px <= 0 or bench_in <= 0:
        return None
    ret = s_out[1] / entry_px - 1
    spy = b_out[1] / bench_in - 1
    return {"entry_day": ed, "entry_kind": kind, "entry_px": entry_px, "exit_day": xd,
            "exit_px": s_out[1], "ret": ret, "spy_ret": spy, "excess": ret - spy}


def summarize(rows: list[tuple[float, float, str]]) -> dict:
    """rows: (p_up, excess, stance). Directional = the panel actually leaned one way."""
    directional = [(p, x, st) for p, x, st in rows if st != "neutral" and p != 0.5]
    signed = [x if p > 0.5 else -x for p, x, _ in directional]
    out = {"n": len(rows), "n_dir": len(directional)}
    if not directional:
        return out
    out["hit_rate"] = sum(s > 0 for s in signed) / len(signed)
    out["mean_signed"] = statistics.fmean(signed)
    if len(signed) > 2:
        sd = statistics.stdev(signed)
        out["t_stat"] = out["mean_signed"] / (sd / math.sqrt(len(signed))) if sd else None
        edges = [p - 0.5 for p, _, _ in directional]
        xs = [x for _, x, _ in directional]
        try:
            out["ic"] = statistics.correlation(edges, xs)
        except statistics.StatisticsError:
            out["ic"] = None
    for stance in ("agree", "split"):
        sub = [(x if p > 0.5 else -x) for p, x, st in directional if st == stance]
        if sub:
            out[stance] = {"n": len(sub), "hit_rate": sum(v > 0 for v in sub) / len(sub),
                           "mean_signed": statistics.fmean(sub)}
    return out


# ---------------- database work ----------------

def refresh_prices(conn, src: PriceSource, horizons: list[int],
                   now: datetime | None = None, tried: set[str] | None = None) -> int:
    """Download only what is new. Once per completed trading day, refresh every pending
    ticker; in between, fetch just tickers that have no bars yet (each tried once per day,
    so symbols with no data, such as OTC names, are not re-requested every cycle)."""
    pending = conn.execute(
        "SELECT p.ticker, min(p.created_at) FROM predictions p "
        "WHERE (SELECT count(*) FROM outcomes o WHERE o.news_id = p.news_id "
        "       AND o.ticker = p.ticker) < %s "
        "  AND p.created_at > now() - interval '45 days' "
        "GROUP BY p.ticker", (len(horizons),)).fetchall()
    if not pending:
        return 0
    tried = tried if tried is not None else set()
    cutoff = completed_cutoff(now)
    first = {t: c.astimezone(ET).date() - timedelta(days=5) for t, c in pending}
    if get_control(conn, "scorer_cutoff") != cutoff.isoformat():
        tickers = set(first) | {BENCHMARK}
        tried.clear()
    else:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM prices_daily WHERE ticker = ANY(%s)", (list(first),))}
        tickers = set(first) - have - tried
    if not tickers:
        return 0
    start = min(first[t] for t in tickers if t in first) if set(first) & tickers else cutoff
    if start > cutoff:
        return 0
    rows = [r for r in src.daily_bars(sorted(tickers), start, cutoff) if r[1] <= cutoff]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO prices_daily (ticker, day, open, close, volume) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (ticker, day) DO UPDATE SET "
            "open = EXCLUDED.open, close = EXCLUDED.close, volume = EXCLUDED.volume", rows)
    tried |= tickers
    set_control(conn, "scorer_cutoff", cutoff.isoformat())
    return len(rows)


def score_pending(conn, horizons: list[int]) -> int:
    days = [r[0] for r in conn.execute(
        "SELECT day FROM prices_daily WHERE ticker = %s ORDER BY day", (BENCHMARK,))]
    if not days:
        return 0
    preds = conn.execute(
        "SELECT p.news_id, p.ticker, p.created_at FROM predictions p "
        "WHERE p.created_at > now() - interval '45 days' "
        "  AND (SELECT count(*) FROM outcomes o WHERE o.news_id = p.news_id "
        "       AND o.ticker = p.ticker) < %s", (len(horizons),)).fetchall()
    if not preds:
        return 0
    tickers = sorted({p[1] for p in preds} | {BENCHMARK})
    px = {(t, d): (o, c) for t, d, o, c in conn.execute(
        "SELECT ticker, day, open, close FROM prices_daily WHERE ticker = ANY(%s)", (tickers,))}
    n = 0
    for news_id, ticker, made_at in preds:
        for h in horizons:
            row = score_one(made_at, ticker, h, days, px)
            if row is None:
                continue
            cur = conn.execute(
                "INSERT INTO outcomes (news_id, ticker, horizon, entry_day, entry_kind, entry_px, "
                "exit_day, exit_px, ret, spy_ret, excess) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (news_id, ticker, h, row["entry_day"], row["entry_kind"], row["entry_px"],
                 row["exit_day"], row["exit_px"], row["ret"], row["spy_ret"], row["excess"]))
            n += cur.rowcount
    return n


def scorecard(conn, horizons: list[int]) -> dict[int, dict]:
    out = {}
    for h in horizons:
        rows = conn.execute(
            "SELECT p.p_up_mean, o.excess, "
            "       coalesce(p.stance, CASE WHEN p.agree THEN 'agree' ELSE 'split' END) "
            "FROM outcomes o JOIN predictions p USING (news_id, ticker) "
            "WHERE o.horizon = %s", (h,)).fetchall()
        out[h] = summarize(rows)
    return out


def run(s: Settings, conn, src: PriceSource | None = None) -> None:
    cfg = s.raw["scorer"]
    if src is None:
        from .prices import AlpacaPrices
        src = AlpacaPrices(s.alpaca_key, s.alpaca_secret)
    failures = 0
    tried: set[str] = set()
    while True:
        try:
            fetched = refresh_prices(conn, src, cfg["horizons"], tried=tried)
            scored = score_pending(conn, cfg["horizons"])
            log.info("scorer: %d bars refreshed, %d outcomes scored", fetched, scored)
            failures = 0
        except Exception as exc:
            failures += 1
            log.exception("scorer cycle failed: %s", exc)
            if failures == 3:
                alert(s.slack_webhook, f"scorekeeper failing repeatedly: {exc}", conn=conn)
        time.sleep(cfg["poll_minutes"] * 60)
