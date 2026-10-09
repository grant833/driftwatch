"""Where did the insider-buy return go? A diagnostic, not a strategy test.

The backtest found no edge from the session after the filing. The research says the
edge (if any) now sits *earlier*: between the insider's trade and the public filing, or in
the first hours after filing. This splits each event's return into legs and grades each
against two yardsticks: SPY (large caps) and IWM (small caps), since many insider-buy
stocks are small companies and SPY may be the wrong comparison.

Legs (excess return vs the benchmark over exactly the same span):
  A. insider's trade-date close -> filing-date close  (we can never earn this)
  B. filing-date close          -> next session's open (needs same-day filing data)
  C. next open (our entry)      -> close after 1, 5 and 20 sessions

Nothing here changes the strategy; it is recorded in the ledger like every other run.
"""
from __future__ import annotations

import bisect
import logging
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from . import ledger
from .backtest import CLIP, DEFAULTS, _t, load_signals
from .bt_data import quarter_of
from .prices import ET

log = logging.getLogger(__name__)

LEGS = ["A: trade -> filing (closed to us)", "B: filing close -> next open",
        "C1: entry -> 1 session", "C5: entry -> 5 sessions", "C20: entry -> 20 sessions"]


def bench_series(conn, ticker: str) -> dict[date, tuple[float, float]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT day, open, close FROM bt_bars WHERE batch = %s AND ticker = %s",
        (ticker, ticker))}


def legs_for(filed: date, trans: date | None, cal: list[date],
             bars: dict[date, tuple]) -> dict[str, tuple]:
    """-> {leg: ((start_day, start_field), (end_day, end_field))}; field 0=open, 2=close."""
    f = bisect.bisect_right(cal, filed) - 1          # filing day, or last session before it
    if f < 0 or f + 1 >= len(cal):
        return {}
    out = {}
    if trans:
        t = bisect.bisect_right(cal, trans) - 1
        if 0 <= t < f:
            out[LEGS[0]] = ((cal[t], 2), (cal[f], 2))
    out[LEGS[1]] = ((cal[f], 2), (cal[f + 1], 0))
    for h, leg in ((1, LEGS[2]), (5, LEGS[3]), (20, LEGS[4])):
        if f + h < len(cal):
            out[leg] = ((cal[f + 1], 0), (cal[f + h], 2))
    return out


def _px(series, day, field):
    row = series.get(day)
    if row is None:
        return None
    return row[0] if field == 0 else row[-1]


def run(conn, root: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(ET)
    cfg = dict(DEFAULTS)
    spy = bench_series(conn, "SPY")
    iwm = bench_series(conn, "IWM")
    cal = sorted(spy)
    signals, _ = load_signals(conn, cfg)
    by_key: dict[tuple, list] = defaultdict(list)
    for s in signals:
        by_key[(s.ticker, quarter_of(s.filed))].append(s)
    results: dict[str, dict[str, list[tuple[str, float]]]] = {
        b: defaultdict(list) for b in ("SPY", "IWM")}
    for (tkr, batch), sigs in by_key.items():
        bars = {r[0]: (r[1], r[3]) for r in conn.execute(
            "SELECT day, open, low, close FROM bt_bars WHERE batch = %s AND ticker = %s",
            (batch, tkr))}
        for s in sigs:
            for leg, ((d0, f0), (d1, f1)) in legs_for(s.filed, s.trans_date, cal,
                                                      bars).items():
                p0, p1 = _px(bars, d0, f0), _px(bars, d1, f1)
                if not p0 or not p1:
                    continue
                r = p1 / p0 - 1
                for name, bench in (("SPY", spy), ("IWM", iwm)):
                    b0, b1 = _px(bench, d0, f0), _px(bench, d1, f1)
                    if b0 and b1:
                        results[name][leg].append((s.filed.strftime("%Y-%m"),
                                                   r - (b1 / b0 - 1)))
    table = {name: {leg: summarize(rows) for leg, rows in legs.items()}
             for name, legs in results.items()}
    out = {"generated_at": now.isoformat(), "signals": len(signals),
           "iwm_available": bool(iwm), "table": table}
    entry = ledger.append(conn, "backtest_diagnostic", {
        "what": "insider buys: excess return by leg vs SPY and IWM",
        "summary": {n: {leg: {k: v for k, v in s.items() if k in ("n", "median", "t_months")}
                        for leg, s in t.items()} for n, t in table.items()}})
    out["ledger"] = entry.seq
    path = root / "backtests" / f"insider-diagnostics-{now:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(out), encoding="utf-8")
    out["path"] = str(path)
    return out


def summarize(rows: list[tuple[str, float]]) -> dict:
    if not rows:
        return {"n": 0}
    xs = [x for _, x in rows]
    months: dict[str, list[float]] = defaultdict(list)
    for m, x in rows:
        months[m].append(max(-CLIP, min(CLIP, x)))
    monthly = [statistics.fmean(v) for v in months.values()]
    return {"n": len(xs), "median": statistics.median(xs),
            "capped_mean": statistics.fmean([max(-CLIP, min(CLIP, x)) for x in xs]),
            "hit_rate": sum(x > 0 for x in xs) / len(xs), "t_months": _t(monthly)}


def render(out: dict) -> str:
    L = [f"# Insider buys: where the return goes ({out['generated_at'][:10]})", "",
         f"{out['signals']:,} signals. Each leg is the stock's return minus the benchmark's "
         "over exactly the same span. Ledger entry "
         f"#{out['ledger']}. A diagnostic, not a strategy test.", ""]
    for name in ("SPY", "IWM"):
        if name == "IWM" and not out["iwm_available"]:
            L += ["(IWM bars not loaded yet: run `backtest-load` once more, then re-run.)", ""]
            continue
        L += [f"## vs {name}", "",
              "| Leg | Events | Beat it | Median | Capped mean | Month t |",
              "|---|---|---|---|---|---|"]
        for leg in LEGS:
            s = out["table"][name].get(leg) or {"n": 0}
            if not s["n"]:
                continue
            t = s["t_months"]
            L.append(f"| {leg} | {s['n']:,} | {s['hit_rate']:.0%} | {s['median']:+.2%} | "
                     f"{s['capped_mean']:+.2%} | {'–' if t is None else f'{t:+.1f}'} |")
        L.append("")
    return "\n".join(L) + "\n"
