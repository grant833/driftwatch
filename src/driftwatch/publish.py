"""Nightly public record: ledger anchor plus the data behind the public dashboard.

Runs once a day (Windows Task Scheduler calls ops/nightly.ps1, which runs this and
then commits anchors/ and docs/ to GitHub). Steps:

  1. Verify the whole hash chain. If it is broken, publish nothing and alert:
     a public record must never paper over tampering or corruption.
  2. Write anchors/YYYY-MM-DD.txt with the chain head. The git commit gives it an
     outside timestamp, so every prediction before it provably existed by then.
  3. Write docs/data/*.json for the GitHub Pages dashboard.

What is deliberately NOT published: news headlines and article text (licensed
Benzinga content; we link to the source instead), API keys, and anything personal.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import ledger
from .alerts import notify
from .db import set_control
from .perf import tournament
from .prices import ET
from .scorer import scorecard

log = logging.getLogger(__name__)

PREDICTION_DAYS = 30        # how much prediction history the dashboard shows
TRADE_LIMIT = 300
# Published list prices, USD per million tokens (input, output). Used for an estimate only.
PRICING = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5-20251001": (1.0, 5.0)}


def clean(x):
    """JSON-safe: NaN/inf become null, datetimes become ISO strings."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x


def est_cost(model: str, tin: int, tout: int) -> float | None:
    p = PRICING.get(model)
    return None if p is None else (tin * p[0] + tout * p[1]) / 1e6


# ---------------- sections ----------------

def ledger_section(conn, now: datetime) -> dict:
    ok, bad, n = ledger.verify(conn)
    h = ledger.head(conn)
    kinds = dict(conn.execute("SELECT kind, count(*) FROM ledger GROUP BY 1").fetchall())
    return {"verified": ok, "first_bad_seq": bad, "entries": n, "by_kind": kinds,
            "verified_at": now, "head_seq": h.seq if h else None,
            "head_hash": h.hash if h else None,
            "head_recorded_at": ledger.ts_str(h.recorded_at) if h else None}


def costs_section(conn) -> dict:
    rows = conn.execute(
        "SELECT (created_at AT TIME ZONE 'America/New_York')::date, model, stage, count(*), "
        "sum(input_tokens), sum(output_tokens) FROM llm_calls "
        "WHERE created_at > now() - interval '30 days' GROUP BY 1, 2, 3 ORDER BY 1").fetchall()
    days: dict[str, float] = {}
    calls = {"triage": 0, "panel": 0}
    for d, model, stage, n, tin, tout in rows:
        c = est_cost(model, tin or 0, tout or 0) or 0.0
        days[d.isoformat()] = days.get(d.isoformat(), 0.0) + c
        calls[stage] = calls.get(stage, 0) + n
    total = sum(days.values())
    avg = total / len(days) if days else 0.0
    return {"daily_usd": [{"day": d, "usd": round(v, 2)} for d, v in days.items()],
            "total_30d_usd": round(total, 2), "avg_daily_usd": round(avg, 2),
            "calls_30d": calls}


def predictions_section(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.created_at, p.ticker, p.p_up_mean, p.p_up_std,
               coalesce(p.stance, CASE WHEN p.agree THEN 'agree' ELSE 'split' END),
               p.magnitude, p.pre_move, p.ledger_seq, n.url, n.published_at,
               (SELECT json_object_agg(o.horizon, o.excess) FROM outcomes o
                 WHERE o.news_id = p.news_id AND o.ticker = p.ticker)
        FROM predictions p JOIN news_items n ON n.id = p.news_id
        WHERE p.created_at > now() - make_interval(days => %s)
        ORDER BY p.created_at DESC
        """, (PREDICTION_DAYS,)).fetchall()
    return [{"made_at": r[0], "ticker": r[1], "p_up": round(r[2], 3), "p_std": round(r[3], 3),
             "stance": r[4], "magnitude": r[5],
             "pre_move": round(r[6], 4) if r[6] is not None else None,
             "seq": r[7], "url": r[8], "published_at": r[9],
             "excess": {str(k): round(v, 4) for k, v in (r[10] or {}).items()}}
            for r in rows]


def trades_section(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT submitted_at, account, ticker, side, qty, limit_price, filled_qty, "
        "filled_avg_price, status, reason, ledger_seq FROM orders "
        "ORDER BY submitted_at DESC LIMIT %s", (TRADE_LIMIT,)).fetchall()
    keys = ("submitted_at", "account", "ticker", "side", "qty", "limit", "filled_qty",
            "fill_price", "status", "reason", "seq")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def positions_section(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT account, ticker, qty, avg_entry_price, current_price, unrealized_plpc, "
        "updated_at FROM positions_live ORDER BY account, market_value DESC").fetchall()
    keys = ("account", "ticker", "qty", "entry", "price", "pl_pct", "as_of")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def equity_section(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for acct, day, eq, spy in conn.execute(
            "SELECT account, day, equity, spy_close FROM equity_daily ORDER BY account, day"):
        out.setdefault(acct, []).append({"day": day, "equity": eq, "spy": spy})
    return out


def tournament_section(conn, trials: int) -> dict:
    stats, waiting, sr0 = tournament(conn, trials)
    for s in stats.values():
        s.pop("r", None)                     # the daily return series is in equity.json
    return {"accounts": stats, "waiting": waiting, "luck_bar_daily_sharpe": sr0,
            "trials": trials}


def build(conn, horizons: list[int], trials: int, now: datetime | None = None) -> dict:
    """Everything the dashboard needs, as {filename: data}."""
    now = now or datetime.now(UTC)
    first = conn.execute("SELECT min(created_at), count(*) FROM predictions").fetchone()
    halted = conn.execute(
        "SELECT value FROM controls WHERE key = 'trading_halted'").fetchone()
    summary = {
        "generated_at": now,
        "generated_et": now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET"),
        "first_prediction_at": first[0],
        "predictions_total": first[1],
        "news_total": conn.execute("SELECT count(*) FROM news_items").fetchone()[0],
        "insider_buys_total": conn.execute(
            "SELECT count(*) FROM insider_buy_signals").fetchone()[0],
        "trading_halted": bool(halted and halted[0] == "true"),
        "ledger": ledger_section(conn, now),
        "scorecard": scorecard(conn, horizons),
        "tournament": tournament_section(conn, trials),
        "costs": costs_section(conn),
    }
    return {"summary.json": summary,
            "predictions.json": predictions_section(conn),
            "trades.json": trades_section(conn),
            "positions.json": positions_section(conn),
            "equity.json": equity_section(conn)}


def write(files: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in files.items():
        p = out_dir / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean(data), indent=1, sort_keys=True, allow_nan=False) + "\n",
                       encoding="utf-8")
        tmp.replace(p)
        paths.append(p)
    return paths


def run(conn, root: Path, horizons: list[int], trials: int,
        now: datetime | None = None) -> list[Path]:
    """Verify, anchor, export. Raises RuntimeError (after alerting) if the chain is broken."""
    now = now or datetime.now(UTC)
    ok, bad, _n = ledger.verify(conn)
    if not ok:
        notify(conn, "problem", f"🚨 Ledger hash chain BROKEN at seq {bad}. "
                                "Nothing was published. Investigate before the next run.")
        raise RuntimeError(f"ledger chain broken at seq {bad}")
    anchor = ledger.anchor(conn, root / "anchors", today=now.astimezone(ET).date())
    paths = write(build(conn, horizons, trials, now), root / "docs" / "data")
    set_control(conn, "last_publish", now.isoformat())
    return ([anchor] if anchor else []) + paths


def stale(now: datetime, last_published: str | None,
          max_age: timedelta = timedelta(hours=50)) -> bool:
    """True if a nightly publish has run before but not recently (so the scheduled
    task has stopped). Never-published returns False: that's a setup step, not a fault."""
    if not last_published:
        return False
    return now - datetime.fromisoformat(last_published) > max_age
