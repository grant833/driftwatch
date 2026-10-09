"""Q5: could a FAST insider strategy work? Measured live, never traded.

The backtest showed the insider-buy edge is mostly priced in by the morning after the
filing. The bot sees each Form 4 within about a minute, so for filings that arrive while
the market is open we record the price at that moment, then grade it (vs IWM, small caps)
at the same day's close, the next open, and 1 and 5 sessions later. If the legs are
reliably positive after realistic costs (~0.2-1% round trip for these stocks), a fast
entry might be worth a pre-registered paper test; if not, insider data is done.
"""
from __future__ import annotations

import bisect
import logging
from datetime import UTC, datetime, timedelta

from .prices import ET, MARKET_CLOSE, MARKET_OPEN, completed_cutoff
from .scorer import summarize

log = logging.getLogger(__name__)
LEGS = ("ex_close", "ex_next_open", "ex_1d", "ex_5d")
LABELS = {"ex_close": "detect -> same-day close", "ex_next_open": "detect -> next open",
          "ex_1d": "detect -> close 1 session later", "ex_5d": "detect -> close 5 sessions later"}
BENCH = "IWM"


def market_open(now: datetime) -> bool:
    et = now.astimezone(ET)
    return et.weekday() < 5 and MARKET_OPEN <= et.time() < MARKET_CLOSE


def record(conn, prices, accession: str, ticker: str, now: datetime | None = None) -> bool:
    """Snapshot the stock and IWM the moment a qualifying buy is detected in market hours."""
    now = now or datetime.now(UTC)
    if not ticker or not market_open(now):
        return False
    try:
        snaps = prices.snapshots([ticker, BENCH])
    except Exception as exc:                      # measurement only: never break the parser
        log.warning("fast-insider snapshot failed for %s: %s", ticker, exc)
        return False
    s, b = snaps.get(ticker), snaps.get(BENCH)
    if not s or not b or not s.get("trade_time"):
        return False
    traded = datetime.fromisoformat(s["trade_time"].replace("Z", "+00:00"))
    if now - traded > timedelta(minutes=15):      # stale quote: halted or illiquid
        return False
    cur = conn.execute(
        "INSERT INTO insider_fast (accession, ticker, detected_at, px, bench_px) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (accession, ticker, now, s["price"], b["price"]))
    return cur.rowcount == 1


def legs(detect_day, px: float, bpx: float, days: list, bars: dict) -> dict:
    """bars: (ticker, day) -> (open, close); returns available excess-return legs."""
    out = {}
    i = bisect.bisect_left(days, detect_day)
    if i >= len(days) or days[i] != detect_day:
        return out

    def ex(day, field):
        s, b = bars.get(("S", day)), bars.get(("B", day))
        if not s or not b:
            return None
        return (s[field] / px - 1) - (b[field] / bpx - 1)

    out["ex_close"] = ex(days[i], 1)
    if i + 1 < len(days):
        out["ex_next_open"] = ex(days[i + 1], 0)
        out["ex_1d"] = ex(days[i + 1], 1)
    if i + 5 < len(days):
        out["ex_5d"] = ex(days[i + 5], 1)
    return {k: v for k, v in out.items() if v is not None}


def grade(conn, src, now: datetime | None = None) -> int:
    rows = conn.execute("SELECT accession, ticker, detected_at, px, bench_px FROM insider_fast "
                        "WHERE graded_at IS NULL").fetchall()
    if not rows:
        return 0
    cutoff = completed_cutoff(now)
    start = min(r[2] for r in rows).astimezone(ET).date()
    if start > cutoff:
        return 0
    tickers = sorted({r[1] for r in rows} | {BENCH})
    raw = src.daily_bars(tickers, start, cutoff)
    by = {(t, d): (o, c) for t, d, o, c, _v in raw}
    days = sorted({d for t, d in by if t == BENCH})
    n = 0
    for acc, tkr, detected, px, bpx in rows:
        bars = {}
        for d in days:
            if (tkr, d) in by:
                bars[("S", d)] = by[(tkr, d)]
            bars[("B", d)] = by[(BENCH, d)]
        got = legs(detected.astimezone(ET).date(), px, bpx, days, bars)
        done = len(got) == len(LEGS) or detected < (now or datetime.now(UTC)) - timedelta(days=30)
        conn.execute(
            "UPDATE insider_fast SET ex_close = %s, ex_next_open = %s, ex_1d = %s, ex_5d = %s, "
            "graded_at = CASE WHEN %s THEN now() END WHERE accession = %s AND ticker = %s",
            (got.get("ex_close"), got.get("ex_next_open"), got.get("ex_1d"), got.get("ex_5d"),
             done, acc, tkr))
        n += bool(got)
    return n


def report_lines(conn) -> list[str]:
    lines = ["Q5 Fast insider entry (price when the bot saw the filing, vs IWM; "
             "costs ~0.2-1% round trip):"]
    for leg in LEGS:
        rows = conn.execute(
            f"SELECT 0.6, {leg}, 'agree', (detected_at AT TIME ZONE 'America/New_York')::date "
            f"FROM insider_fast WHERE {leg} IS NOT NULL").fetchall()
        c = summarize(rows)
        if not c.get("n_dir"):
            lines.append(f"  {LABELS[leg]:34s} no data yet")
            continue
        td = c.get("t_days")
        lines.append(f"  {LABELS[leg]:34s} n={c['n_dir']:<4d} up {c['hit_rate']:.0%}  "
                     f"med {c['median_signed']:+.2%}  avg {c['mean_signed']:+.2%}  "
                     f"by-day t {'–' if td is None else f'{td:+.1f}'}")
    return lines
