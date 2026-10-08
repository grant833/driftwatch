"""Trading strategies, position sizing and exit rules. Pure logic plus read-only queries;
the trader service is the only thing that places orders.

  news_drift     (account A): buy when the AI panel AGREES the stock will beat SPY
                              (p_up >= 0.5 + min_edge) and the move isn't already priced in.
  insider_follow (account B): buy after discretionary open-market purchases by officers or
                              directors (the documented "opportunistic insider" signal).
  spy_trend      (account C): hold SPY while it is above its 200-day average, else cash.
                              No AI at all: the bar the other two have to clear.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class Candidate:
    ticker: str
    reason: str
    score: float
    ref_price: float | None = None
    ref_news_id: int | None = None
    ledger_seq: int | None = None
    cik: str | None = None


@dataclass
class Exit:
    ticker: str
    reason: str


# ---------------- pure helpers ----------------

def add_trading_days(d: date, n: int) -> date:
    """Weekdays only; exchange holidays just make a hold one day longer."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def daily_vol(closes: list[float]) -> float | None:
    rets = [b / a - 1 for a, b in zip(closes, closes[1:], strict=False) if a > 0]
    return statistics.stdev(rets) if len(rets) >= 10 else None


def position_dollars(equity: float, vol: float | None, cfg: dict, tcfg: dict) -> float:
    """Equal slots, scaled down for jumpy stocks: a stock twice as volatile as the
    reference gets half a slot. Never above max_position_pct of equity."""
    vol = vol or tcfg["default_daily_vol"]
    slot = 1.0 / cfg["max_positions"]
    scale = min(1.0, tcfg["reference_daily_vol"] / max(vol, 1e-4))
    return equity * min(cfg["max_position_pct"], slot * scale)


def shares_for(dollars: float, price: float) -> int:
    return int(dollars // price) if price > 0 else 0


def limit_price(price: float, side: str, slip: float) -> float:
    p = price * (1 + slip) if side == "buy" else price * (1 - slip)
    return round(p, 2) if p >= 1 else round(p, 4)


def trend_on(closes: list[float], sma_days: int) -> bool | None:
    if len(closes) < sma_days:
        return None
    return closes[-1] > sum(closes[-sma_days:]) / sma_days


def drawdown(equity: float, peak: float) -> float:
    return equity / peak - 1 if peak > 0 else 0.0


# ---------------- candidate queries ----------------

def news_candidates(conn, cfg: dict, account: str) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT p.ticker, p.news_id, p.ledger_seq, p.p_up_mean, p.novelty_mean, p.pre_move,
               p.ref_price, n.headline
        FROM predictions p JOIN news_items n ON n.id = p.news_id
        WHERE p.created_at > now() - make_interval(hours => %s)
          AND p.stance = 'agree' AND p.p_up_mean >= 0.5 + %s
          AND (p.pre_move IS NULL OR p.pre_move <= %s)
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.account = %s AND o.ticker = p.ticker
                          AND o.side = 'buy' AND o.submitted_at > now() - interval '5 days')
        ORDER BY p.p_up_mean DESC, p.novelty_mean DESC
        """, (cfg["entry_window_hours"], cfg["min_edge"], cfg["max_pre_move"], account)
    ).fetchall()
    out, seen = [], set()
    for tkr, nid, seq, p, nov, _pre, ref, headline in rows:
        if tkr in seen:
            continue
        seen.add(tkr)
        out.append(Candidate(tkr, f"news p_up={p:.2f}: {headline[:80]}", p + 0.01 * nov,
                             ref, nid, seq))
    return out


def insider_candidates(conn, cfg: dict, account: str) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT s.ticker, count(DISTINCT s.insider_name), sum(s.value_usd), max(s.price),
               bool_or(p.news_id IS NOT NULL), max(s.issuer_cik)
        FROM insider_buy_signals s
        LEFT JOIN predictions p ON p.ticker = s.ticker AND p.stance = 'agree'
             AND p.p_up_mean > 0.5 AND p.created_at > now() - interval '7 days'
        WHERE s.received_at > now() - make_interval(days => %s)
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.account = %s AND o.ticker = s.ticker
                          AND o.side = 'buy' AND o.submitted_at > now() - interval '30 days')
        GROUP BY s.ticker
        """, (cfg["lookback_days"], account)).fetchall()
    out = []
    for tkr, n_ins, value, px, news_ok, cik in rows:
        score = n_ins + math.log10(max(value or 1, 1)) / 10 + (1 if news_ok else 0)
        why = f"insider buys: {n_ins} insider(s), ${value:,.0f}" + (" + bullish news" if news_ok
                                                                     else "")
        out.append(Candidate(tkr, why, score, px, cik=cik))
    return sorted(out, key=lambda c: -c.score)


def contradicted(conn, account: str) -> set[str]:
    """Held tickers where the panel has since agreed the outlook turned negative."""
    rows = conn.execute(
        """
        SELECT DISTINCT h.ticker FROM holdings h JOIN predictions p ON p.ticker = h.ticker
        WHERE h.account = %s AND p.stance = 'agree' AND p.p_up_mean <= 0.45
          AND (p.created_at AT TIME ZONE 'America/New_York')::date >= h.entry_day
        """, (account,)).fetchall()
    return {r[0] for r in rows}


def plan_exits(positions, holdings: dict[str, dict], today: date, stop_loss: float,
               contradicted_set: set[str]) -> list[Exit]:
    """Only positions this account opened (present in holdings) are managed."""
    exits = []
    for p in positions:
        h = holdings.get(p.ticker)
        if h is None:
            continue
        if p.unrealized_plpc <= -stop_loss:
            exits.append(Exit(p.ticker, f"stop-loss ({p.unrealized_plpc:+.1%})"))
        elif today >= h["exit_after"]:
            exits.append(Exit(p.ticker, f"time exit (held since {h['entry_day']})"))
        elif p.ticker in contradicted_set:
            exits.append(Exit(p.ticker, "panel turned bearish"))
    return exits


# SEC industry codes that aren't operating companies: open-end funds, closed-end funds,
# and blank-check companies (SPACs). Insider buying there isn't the same signal.
NOT_OPERATING_SIC = {"6722", "6726", "6770"}


def operating_company(conn, cik: str | None, sec=None) -> bool:
    """True unless the SEC classifies the issuer as a fund or SPAC. Looks the company
    up once (via `sec`) and caches it; unknown issuers are skipped to be safe."""
    if not cik:
        return False
    cik = str(cik).zfill(10)
    row = conn.execute("SELECT sic FROM sec_companies WHERE cik = %s", (cik,)).fetchone()
    if row is None:
        if sec is None:
            return False
        try:
            sic, desc = sec(cik)
        except Exception:
            return False
        conn.execute("INSERT INTO sec_companies (cik, sic, sic_desc) VALUES (%s, %s, %s) "
                     "ON CONFLICT (cik) DO NOTHING", (cik, sic, desc))
        row = (sic,)
    return str(row[0] or "") not in NOT_OPERATING_SIC
