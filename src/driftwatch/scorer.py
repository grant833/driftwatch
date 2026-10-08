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
from .perf import beta_of
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


BETA_LOOKBACK = 100


def pre_entry_beta(ticker: str, entry_day: date, days: list[date],
                   px: dict[tuple[str, date], tuple[float, float]]) -> float | None:
    """Beta vs SPY from up to 100 sessions strictly before entry (nothing after it)."""
    i = bisect.bisect_left(days, entry_day)
    window = [d for d in days[max(0, i - BETA_LOOKBACK - 1):i]
              if (ticker, d) in px and (BENCHMARK, d) in px]
    return beta_of([px[(ticker, d)][1] for d in window],
                   [px[(BENCHMARK, d)][1] for d in window])


def _ranks(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=v.__getitem__)
    r = [0.0] * len(v)
    i = 0
    while i < len(v):                       # average ranks for ties
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def _t(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(len(xs))) if sd else None


CLIP = 0.10   # cap each call's result at +-10% so one wild stock can't fake an edge


def summarize(rows: list[tuple]) -> dict:
    """rows: (p_up, excess, stance[, entry_day]). Directional = the panel leaned one way.

    Beyond the plain average, reports outlier-resistant views (median, capped mean, rank IC)
    and a by-day t-stat: calls made on the same day share market moves, so the honest
    sample size is the number of distinct days, not the number of calls."""
    rows = [tuple(r) + (None,) * (4 - len(r)) for r in rows
            if r[0] is not None and r[1] is not None]
    directional = [r for r in rows if r[2] != "neutral" and r[0] != 0.5]
    signed = [x if p > 0.5 else -x for p, x, _, _ in directional]
    out = {"n": len(rows), "n_dir": len(directional)}
    if not directional:
        return out
    out["hit_rate"] = sum(v > 0 for v in signed) / len(signed)
    out["mean_signed"] = statistics.fmean(signed)
    out["median_signed"] = statistics.median(signed)
    out["clipped_mean"] = statistics.fmean([max(-CLIP, min(CLIP, v)) for v in signed])
    out["t_stat"] = _t(signed)
    days: dict = {}
    for (_p, _x, _st, d), v in zip(directional, signed, strict=True):
        days.setdefault(d, []).append(max(-CLIP, min(CLIP, v)))
    out["n_days"] = len([d for d in days if d is not None])
    out["t_days"] = _t([statistics.fmean(v) for d, v in days.items() if d is not None])
    if len(directional) > 2:
        edges = [p - 0.5 for p, _, _, _ in directional]
        xs = [x for _, x, _, _ in directional]
        try:
            out["ic"] = statistics.correlation(edges, xs)
            out["rank_ic"] = statistics.correlation(_ranks(edges), _ranks(xs))
        except statistics.StatisticsError:
            out["ic"] = out["rank_ic"] = None
    for stance in ("agree", "split"):
        sub = [v for r, v in zip(directional, signed, strict=True) if r[2] == stance]
        if sub:
            out[stance] = {"n": len(sub), "hit_rate": sum(v > 0 for v in sub) / len(sub),
                           "mean_signed": statistics.fmean(sub),
                           "median_signed": statistics.median(sub)}
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
    # ~150 calendar days of history before each call, for the pre-entry beta
    first = {t: c.astimezone(ET).date() - timedelta(days=150) for t, c in pending}
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
            beta = pre_entry_beta(ticker, row["entry_day"], days, px)
            abnormal = row["ret"] - beta * row["spy_ret"] if beta is not None else None
            cur = conn.execute(
                "INSERT INTO outcomes (news_id, ticker, horizon, entry_day, entry_kind, entry_px, "
                "exit_day, exit_px, ret, spy_ret, excess, beta, abnormal) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (news_id, ticker, h, row["entry_day"], row["entry_kind"], row["entry_px"],
                 row["exit_day"], row["exit_px"], row["ret"], row["spy_ret"], row["excess"],
                 beta, abnormal))
            n += cur.rowcount
    return n


def backfill_beta(conn) -> int:
    """Add beta-adjusted results to outcomes graded before beta existed (or before enough
    price history had been downloaded)."""
    todo = conn.execute("SELECT news_id, ticker, horizon, entry_day, ret, spy_ret FROM outcomes "
                        "WHERE abnormal IS NULL AND scored_at > now() - interval '60 days'"
                        ).fetchall()
    if not todo:
        return 0
    days = [r[0] for r in conn.execute(
        "SELECT day FROM prices_daily WHERE ticker = %s ORDER BY day", (BENCHMARK,))]
    tickers = sorted({r[1] for r in todo} | {BENCHMARK})
    px = {(t, d): (o, c) for t, d, o, c in conn.execute(
        "SELECT ticker, day, open, close FROM prices_daily WHERE ticker = ANY(%s)", (tickers,))}
    n = 0
    for news_id, ticker, h, entry_day, ret, spy_ret in todo:
        beta = pre_entry_beta(ticker, entry_day, days, px)
        if beta is None:
            continue
        conn.execute("UPDATE outcomes SET beta = %s, abnormal = %s WHERE news_id = %s "
                     "AND ticker = %s AND horizon = %s",
                     (beta, ret - beta * spy_ret, news_id, ticker, h))
        n += 1
    return n


def scorecard(conn, horizons: list[int]) -> dict[int, dict]:
    """Per horizon: stats on excess return vs SPY, plus the same stats on the
    beta-adjusted ("abnormal") return under the key 'beta_adj'."""
    out = {}
    for h in horizons:
        rows = conn.execute(
            "SELECT p.p_up_mean, o.excess, "
            "       coalesce(p.stance, CASE WHEN p.agree THEN 'agree' ELSE 'split' END), "
            "       o.entry_day, o.abnormal "
            "FROM outcomes o JOIN predictions p USING (news_id, ticker) "
            "WHERE o.horizon = %s", (h,)).fetchall()
        out[h] = summarize([r[:4] for r in rows])
        adj = summarize([(r[0], r[4], r[2], r[3]) for r in rows])
        if adj.get("n_dir"):
            out[h]["beta_adj"] = {k: adj.get(k) for k in (
                "n_dir", "hit_rate", "median_signed", "clipped_mean", "t_days", "rank_ic")}
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
            adjusted = backfill_beta(conn)
            log.info("scorer: %d bars refreshed, %d outcomes scored, %d beta-adjusted",
                     fetched, scored, adjusted)
            failures = 0
        except Exception as exc:
            failures += 1
            log.exception("scorer cycle failed: %s", exc)
            if failures == 3:
                alert(s.slack_webhook, f"scorekeeper failing repeatedly: {exc}", conn=conn)
        time.sleep(cfg["poll_minutes"] * 60)
