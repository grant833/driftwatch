"""Historical backtest of the insider strategy (the `insider` paper account's rules).

Two views, both point-in-time honest:

  * Event study: every qualifying insider purchase since 2016, bought at the OPEN of the
    first trading day after the Form 4 was filed (never the trade date: the public can't
    know about a purchase until the filing), graded vs SPY over 5, 10 and 20 sessions.
  * Portfolio simulation: a $100k account run day by day with the live rules: ranking,
    12 slots, 10% max per stock, volatility-scaled sizes, 95% max invested, a 5% "don't
    chase" limit, 10% stop-loss, 20-session time exit, trading costs.

Guardrails against fooling ourselves:
  * Only data dated before each decision is used (volatility and beta come from bars
    before entry; signals become tradeable the session after their filing date).
  * The configuration is written to the hash-chained ledger BEFORE results are computed,
    and the results are written after, so every run is on the record (no quiet retries).
  * Exploratory slices (cluster buys, big buys, officers) are labelled as such: with
    enough slicing something always looks good by chance.

Known limitations (stated in the report):
  * Survivorship: stocks Alpaca has no bars for (many delisted names) drop out. Delisted
    companies skew toward bad outcomes, so results may be somewhat optimistic.
  * Rule 10b5-1 plan flags only exist in filings since 2023 (excluded when present).
  * The live account also gives a bonus to stocks with bullish AI news; history can't.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import ledger
from .perf import beta_of, max_drawdown, psr, sharpe
from .prices import ET
from .strategies import NOT_OPERATING_SIC, daily_vol

log = logging.getLogger(__name__)

DEFAULTS = {
    "start": "2016-01-01",
    "min_value_usd": 25000,      # per transaction, like the live insider filter
    "min_price": 5.0,            # insider's reported price per share
    "max_chase": 0.05,           # skip if the stock already ran >5% past the insider's price
    "lookback_sessions": 3,      # a signal stays tradeable for 3 sessions (live: 3 days)
    "max_positions": 12,
    "max_position_pct": 0.10,
    "max_invested": 0.95,
    "reference_daily_vol": 0.02,
    "default_daily_vol": 0.03,
    "hold_sessions": 20,
    "stop_loss": 0.10,
    "cost_bps": 10,              # per side: commission-free, but spread and slippage are real
    "rebuy_cooldown_days": 30,
    "risk_off_slots": 0.5,       # half the slots while SPY is below its 200-day average
    "sma_days": 200,
    "halt_daily_loss": 0.03,     # no new entries on a day the account is down 3%+
    "halt_drawdown": 0.15,       # no new entries after a 15% drop from the peak...
    "review_sessions": 5,        # ...until reviewed (live: you send /resume; here: 5 sessions)
    "horizons": [5, 10, 20],
    "initial_equity": 100000.0,
}
CLIP = 0.25           # cap each event's result at +-25% in the "capped mean"
BETA_LOOKBACK = 100   # sessions before entry used to estimate beta


# ---------------- data shapes ----------------

@dataclass
class Signal:
    ticker: str
    filed: date
    trans_date: date | None
    n_insiders: int
    value: float
    officer: bool          # at least one buyer is an officer (not only directors)
    top_officer: bool      # CEO / CFO / President / Chair among the buyers
    owners: frozenset = frozenset()   # reporting owners' CIKs (to count people, not filings)


@dataclass
class Event:
    sig: Signal
    entry_idx: int                       # index into the trading calendar
    ref_close: float | None              # close on/just before the insider's trade date
    vol: float | None
    beta: float | None
    path: list[tuple] = field(repr=False, default_factory=list)  # (idx, open, low, close)
    ret: dict = field(default_factory=dict)       # h -> stock return
    spy: dict = field(default_factory=dict)       # h -> SPY return, same window
    delisted: bool = False                        # bars ended before the longest horizon


# CEO, CFO, President (not Vice President), Chair: matched per title, word by word.
TOP_TITLE = re.compile(r"\b(CEO|CFO|CHIEF EXECUTIVE|CHIEF FINANCIAL|(?<!VICE )(?<!VICE-)"
                       r"PRESIDENT|CHAIR(MAN|WOMAN|PERSON)?)\b")


# ---------------- signals ----------------

def load_signals(conn, cfg: dict) -> tuple[list[Signal], dict]:
    """Group qualifying purchases by (ticker, filing date), mirroring the live view
    insider_buy_signals: code P, acquired, officer or director, >= min value.

    Two plain queries, then one pass in Python (a self-join in SQL over ~10 years of
    filings is far too slow)."""
    owners: dict[str, list[tuple]] = defaultdict(list)
    for acc, cik, officer, title in conn.execute(
            "SELECT accession, owner_cik, is_officer, upper(coalesce(title, '')) "
            "FROM bt_insider_owners WHERE is_officer OR is_director"):
        owners[acc].append((cik, officer, title))
    trades = conn.execute(
        "SELECT accession, ticker, filing_date, trans_date, shares, price, value_usd, "
        "lpad(issuer_cik, 10, '0') FROM bt_insider_trades "
        "WHERE doc_type = '4' AND ticker IS NOT NULL AND value_usd >= %s AND price >= %s "
        "AND filing_date >= %s AND NOT plan_10b5_1 ORDER BY accession",
        (cfg["min_value_usd"], cfg["min_price"], cfg["start"])).fetchall()
    # One purchase reported on several Forms 4 that share a reporting owner (e.g. an
    # officer and their family trust filing jointly) counts once. Different people
    # buying identical lots on the same day are separate purchases.
    seen: dict[tuple, set[str]] = defaultdict(set)
    groups: dict[tuple, dict] = {}
    for acc, tkr, filed, tdate, shares, price, value, cik in trades:
        own = owners.get(acc)
        if not own:
            continue                       # no officer or director on this filing
        g = groups.setdefault((tkr, filed), {"value": 0.0, "trans": None, "cik": cik,
                                             "owners": {}, "accs": set()})
        if acc not in g["accs"]:
            g["accs"].add(acc)
            for o_cik, officer, title in own:
                prev = g["owners"].get(o_cik, (False, ""))
                g["owners"][o_cik] = (prev[0] or officer, prev[1] or title)
        ids = {o[0] for o in own}
        key = (tkr, filed, tdate, shares, price)
        dup = bool(seen[key] & ids) and acc not in seen.get((key, "accs"), set())
        seen[key] |= ids
        seen.setdefault((key, "accs"), set()).add(acc)
        if dup:
            continue
        g["value"] += value or 0.0
        if tdate and (g["trans"] is None or tdate > g["trans"]):
            g["trans"] = tdate
    funds = {r[0] for r in conn.execute("SELECT ticker FROM fund_tickers")}
    sic = dict(conn.execute("SELECT cik, sic FROM sec_companies").fetchall())
    out, skipped = [], defaultdict(int)
    for (tkr, filed), g in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if tkr in funds:
            skipped["fund ticker"] += 1
            continue
        if str(sic.get(g["cik"]) or "") in NOT_OPERATING_SIC:
            skipped["fund or SPAC (SEC industry code)"] += 1
            continue
        people = g["owners"]
        top = any(TOP_TITLE.search(t) for _, t in people.values() if t)
        out.append(Signal(tkr, filed, g["trans"], len(people), g["value"],
                          any(o for o, _ in people.values()), top, frozenset(people)))
    log.info("signals: %d from %d qualifying purchases", len(out), len(trades))
    return out, dict(skipped)


# ---------------- events (pure, unit tested) ----------------

def build_event(sig: Signal, cal: list[date], bars: dict[date, tuple],
                spy: dict[date, tuple], cfg: dict) -> tuple[Event | None, str]:
    """bars/spy: day -> (open, low, close). Returns (event, '') or (None, skip reason)."""
    i = bisect.bisect_right(cal, sig.filed)          # first session strictly after filing
    if i >= len(cal):
        return None, "too recent"
    entry_day = cal[i]
    if entry_day not in bars:
        return None, "no price data"
    before = [d for d in cal[max(0, i - BETA_LOOKBACK - 1):i] if d in bars and d in spy]
    closes = [bars[d][2] for d in before]
    vol = daily_vol(closes[-21:]) if len(closes) >= 11 else None
    beta = beta_of(closes, [spy[d][2] for d in before])
    ref = None
    if sig.trans_date:   # proxy for the insider's price, on the same adjusted basis as entry
        prior = [d for d in before if d <= sig.trans_date]
        ref = bars[prior[-1]][2] if prior else None   # None: trade predates the window
    span = cal[i:i + cfg["hold_sessions"] + cfg["lookback_sessions"] + 3]
    path = [(i + k, *bars[d]) for k, d in enumerate(span) if d in bars]
    ev = Event(sig, i, ref, vol, beta, path)
    entry_open = bars[entry_day][0]
    if entry_open <= 0:
        return None, "bad price"
    longest = i + max(cfg["horizons"]) - 1
    ev.delisted = longest < len(cal) and path[-1][0] < longest
    for h in cfg["horizons"]:
        xi = i + h - 1
        if xi >= len(cal):
            continue                               # outcome not known yet
        got = [p for p in path if p[0] <= xi]
        spy_in, spy_out = spy.get(entry_day), spy.get(cal[xi])
        if not spy_in or not spy_out:
            continue
        ev.ret[h] = got[-1][3] / entry_open - 1    # last close on/before the exit session
        ev.spy[h] = spy_out[2] / spy_in[0] - 1
    return ev, ""


def chase_ok(entry_open: float, ref: float | None, max_chase: float) -> bool:
    return ref is None or entry_open <= ref * (1 + max_chase)


# ---------------- statistics ----------------

def _t(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(len(xs))) if sd else None


def study(events: list[Event], h: int) -> dict:
    rows = [e for e in events if h in e.ret]
    if not rows:
        return {"n": 0}
    ex = [e.ret[h] - e.spy[h] for e in rows]
    ab = [e.ret[h] - e.beta * e.spy[h] for e in rows if e.beta is not None]
    months: dict[str, list[float]] = defaultdict(list)
    for e, x in zip(rows, ex, strict=True):
        months[e.sig.filed.strftime("%Y-%m")].append(max(-CLIP, min(CLIP, x)))
    monthly = [statistics.fmean(v) for v in months.values()]
    return {"n": len(rows), "mean_excess": statistics.fmean(ex),
            "median_excess": statistics.median(ex),
            "capped_mean": statistics.fmean([max(-CLIP, min(CLIP, x)) for x in ex]),
            "hit_rate": sum(x > 0 for x in ex) / len(ex),
            "mean_abnormal": statistics.fmean(ab) if ab else None,
            "n_months": len(monthly), "t_months": _t(monthly),
            "pct_months_positive": sum(m > 0 for m in monthly) / len(monthly)}


def by_year(events: list[Event], h: int) -> dict[int, dict]:
    yrs: dict[int, list[Event]] = defaultdict(list)
    for e in events:
        yrs[e.sig.filed.year].append(e)
    out = {}
    for y, evs in sorted(yrs.items()):
        ex = [e.ret[h] - e.spy[h] for e in evs if h in e.ret]
        if ex:
            out[y] = {"n": len(ex), "median_excess": statistics.median(ex),
                      "capped_mean": statistics.fmean([max(-CLIP, min(CLIP, x)) for x in ex]),
                      "hit_rate": sum(x > 0 for x in ex) / len(ex)}
    return out


SLICES = {
    "all signals (pre-registered)": lambda e: True,
    "cluster: 2+ insiders": lambda e: e.sig.n_insiders >= 2,
    "big: $250k+": lambda e: e.sig.value >= 250_000,
    "CEO/CFO/President/Chair": lambda e: e.sig.top_officer,
    "directors only": lambda e: not e.sig.officer,
}


# ---------------- portfolio simulation ----------------

def simulate(events: list[Event], cal: list[date], spy: dict[date, tuple], cfg: dict) -> dict:
    """Day-by-day account with the live rules. Decisions on day i use only data through
    day i-1 (equity, volatility) plus that day's open for fills."""
    cost = cfg["cost_bps"] / 1e4
    by_start: dict[int, list[Event]] = defaultdict(list)
    for e in events:
        for k in range(cfg["lookback_sessions"]):
            by_start[e.entry_idx + k].append(e)
    first = min((e.entry_idx for e in events), default=None)
    if first is None:
        return {}
    last = max(i for i, d in enumerate(cal) if d in spy)
    cash = cfg["initial_equity"]
    spy_closes = [spy[d][2] if d in spy else None for d in cal]
    for k in range(1, len(spy_closes)):              # fill rare calendar gaps
        if spy_closes[k] is None:
            spy_closes[k] = spy_closes[k - 1]
    peak, halted_until, halts, blocked_days = cash, None, 0, 0
    held: dict[str, dict] = {}          # ticker -> {shares, entry_px, entry_idx, path, last}
    last_buy: dict[str, date] = {}
    trades, curve, exposure = [], [], []
    prev_equity = cash

    def bar(pos, i):
        return pos["bars"].get(i)

    for i in range(first, last + 1):
        day = cal[i]
        # 1) morning: time exits and gap-down stops at the open; delisted names at last close
        for t in list(held):
            pos = held[t]
            b = bar(pos, i)
            stop_px = pos["entry_px"] * (1 - cfg["stop_loss"])
            why = px = None
            if b is None:
                if i > pos["last_idx"]:                  # no more data: delisted/halted
                    why, px = "data ended", pos["last_close"]
            elif i >= pos["entry_idx"] + cfg["hold_sessions"]:
                why, px = "time", b[0]
            elif b[0] <= stop_px:
                why, px = "stop (gap)", b[0]
            if why:
                cash += pos["shares"] * px * (1 - cost)
                trades.append({"ticker": t, "entry": cal[pos["entry_idx"]].isoformat(),
                               "exit": day.isoformat(), "why": why,
                               "ret": px * (1 - cost) / (pos["entry_px"] * (1 + cost)) - 1})
                del held[t]
        # 2) entries at the open, unless a live safety rule blocks them today
        open_equity = cash + sum(p["shares"] * (bar(p, i) or (p["last_close"],))[0]
                                 for p in held.values())
        peak = max(peak, prev_equity)
        if halted_until is None and open_equity / peak - 1 <= -cfg["halt_drawdown"]:
            halted_until = i + cfg["review_sessions"]
            halts += 1
        if halted_until is not None and i >= halted_until:
            halted_until, peak = None, open_equity          # reviewed and resumed
        loss_brake = open_equity / prev_equity - 1 <= -cfg["halt_daily_loss"]
        sma_closes = spy_closes[max(0, i - cfg["sma_days"]):i]          # through yesterday
        risk_off = (len(sma_closes) == cfg["sma_days"]
                    and sma_closes[-1] < sum(sma_closes) / len(sma_closes))
        slots = int(cfg["max_positions"] * (cfg["risk_off_slots"] if risk_off else 1))
        blocked = halted_until is not None or loss_brake
        blocked_days += blocked
        # Signals for the same stock inside the lookback window add up, like live.
        pool: dict[str, list[Event]] = defaultdict(list)
        for e in by_start.get(i, []):
            pool[e.sig.ticker].append(e)
        def people(evs):
            ids = set().union(*(e.sig.owners for e in evs))
            return len(ids) if ids else max(e.sig.n_insiders for e in evs)

        cands = sorted(pool.items(), key=lambda kv: -(
            people(kv[1]) + math.log10(max(sum(e.sig.value for e in kv[1]), 1)) / 10))
        invested = sum(p["shares"] * p["last_close"] for p in held.values())
        for t, evs in ([] if blocked else cands):
            if len(held) >= slots:
                break
            if t in held or (t in last_buy and (day - last_buy[t]).days
                             < cfg["rebuy_cooldown_days"]):
                continue
            # Each filing's price reference is compared with today's open from the SAME
            # fetch (same adjustment basis); use the freshest filing that passes.
            pick = None
            for x in sorted(evs, key=lambda x: -x.entry_idx):
                bar_i = {p[0]: p for p in x.path}.get(i)
                if bar_i is not None and chase_ok(bar_i[1], x.ref_close, cfg["max_chase"]):
                    pick = (x, bar_i)
                    break
            if pick is None:
                continue
            e, todays = pick
            vol = e.vol or cfg["default_daily_vol"]
            frac = min(cfg["max_position_pct"], (1 / cfg["max_positions"])
                       * min(1.0, cfg["reference_daily_vol"] / max(vol, 1e-4)))
            dollars = min(prev_equity * frac, cash,
                          cfg["max_invested"] * prev_equity - invested)
            px = todays[1] * (1 + cost)
            shares = int(dollars // px) if px > 0 and dollars > 0 else 0
            if shares < 1:
                continue
            cash -= shares * px
            invested += shares * todays[1]
            held[t] = {"shares": shares, "entry_px": todays[1], "entry_idx": i,
                       "bars": {p[0]: (p[1], p[2], p[3]) for p in e.path},
                       "last_idx": e.path[-1][0], "last_close": todays[1]}
            last_buy[t] = day
        # 3) close: stop-loss on the close, then mark to market
        for t in list(held):
            pos = held[t]
            b = bar(pos, i)
            if b is None:
                continue
            pos["last_close"] = b[2]
            if b[2] <= pos["entry_px"] * (1 - cfg["stop_loss"]):
                cash += pos["shares"] * b[2] * (1 - cost)
                trades.append({"ticker": t, "entry": cal[pos["entry_idx"]].isoformat(),
                               "exit": day.isoformat(), "why": "stop",
                               "ret": b[2] * (1 - cost) / (pos["entry_px"] * (1 + cost)) - 1})
                del held[t]
        mv = sum(p["shares"] * p["last_close"] for p in held.values())
        equity = cash + mv
        if day in spy:
            curve.append((day, equity, spy[day][2]))
            exposure.append(mv / equity if equity > 0 else 0.0)
        prev_equity = equity
    return {"curve": curve, "trades": trades, "exposure": exposure,
            "drawdown_halts": halts, "blocked_days": blocked_days}


def portfolio_stats(sim: dict) -> dict:
    curve = sim.get("curve") or []
    if len(curve) < 30:
        return {}
    eq = [c[1] for c in curve]
    sp = [c[2] for c in curve]
    r = [b / a - 1 for a, b in zip(eq, eq[1:], strict=False)]
    rs = [b / a - 1 for a, b in zip(sp, sp[1:], strict=False)]
    expo = sim["exposure"]
    # Fair benchmark for a part-cash strategy: SPY held at the same exposure each day.
    matched = [e * x for e, x in zip(expo, rs, strict=False)]
    active = [a - b for a, b in zip(r, rs, strict=True)]
    active_m = [a - b for a, b in zip(r, matched, strict=True)]
    years = (curve[-1][0] - curve[0][0]).days / 365.25

    def cagr(series):
        return (series[-1] / series[0]) ** (1 / years) - 1 if years > 0 else None

    m_curve = [1.0]
    for x in matched:
        m_curve.append(m_curve[-1] * (1 + x))
    yearly: dict[int, dict] = {}
    prev = (eq[0], sp[0])
    for k, (d, e, s_) in enumerate(curve):
        if k + 1 == len(curve) or curve[k + 1][0].year != d.year:   # last session of a year
            yearly[d.year] = {"strategy": e / prev[0] - 1, "spy": s_ / prev[1] - 1}
            prev = (e, s_)
    tr = sim["trades"]
    ann = math.sqrt(252)
    sr, srs = sharpe(r), sharpe(rs)
    return {
        "start": curve[0][0].isoformat(), "end": curve[-1][0].isoformat(),
        "years": round(years, 2),
        "total_return": eq[-1] / eq[0] - 1, "spy_total_return": sp[-1] / sp[0] - 1,
        "cagr": cagr(eq), "spy_cagr": cagr(sp), "matched_spy_cagr": cagr(m_curve),
        "ann_vol": statistics.stdev(r) * ann, "spy_ann_vol": statistics.stdev(rs) * ann,
        "sharpe": sr * ann if sr else None, "spy_sharpe": srs * ann if srs else None,
        "max_drawdown": max_drawdown(eq), "spy_max_drawdown": max_drawdown(sp),
        "avg_exposure": statistics.fmean(expo),
        "p_beats_spy": psr(active), "p_beats_matched_spy": psr(active_m),
        "p_sharpe_positive": psr(r),
        "trades": len(tr),
        "win_rate": sum(t["ret"] > 0 for t in tr) / len(tr) if tr else None,
        "avg_trade": statistics.fmean([t["ret"] for t in tr]) if tr else None,
        "median_trade": statistics.median([t["ret"] for t in tr]) if tr else None,
        "exits": {k: sum(t["why"] == k for t in tr) for k in sorted({t["why"] for t in tr})},
        "drawdown_halts": sim.get("drawdown_halts", 0),
        "days_entries_blocked": sim.get("blocked_days", 0),
        "yearly": yearly,
    }


# ---------------- orchestration ----------------

def load_calendar(conn) -> tuple[list[date], dict[date, tuple]]:
    rows = conn.execute("SELECT day, open, low, close FROM bt_bars WHERE batch = 'SPY' "
                        "AND ticker = 'SPY' ORDER BY day").fetchall()
    return [r[0] for r in rows], {r[0]: (r[1], r[2], r[3]) for r in rows}


def build_events(conn, signals: list[Signal], cal, spy, cfg) -> tuple[list[Event], dict]:
    from .bt_data import quarter_of
    by_ticker: dict[tuple[str, str], list[Signal]] = defaultdict(list)
    for s in signals:
        by_ticker[(s.ticker, quarter_of(s.filed))].append(s)
    events, skipped = [], defaultdict(int)
    for n, ((tkr, batch), sigs) in enumerate(sorted(by_ticker.items())):
        bars = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT day, open, low, close FROM bt_bars WHERE batch = %s AND ticker = %s",
            (batch, tkr))}
        for s in sigs:
            ev, why = build_event(s, cal, bars, spy, cfg)
            if ev is None:
                skipped[why] += 1
            elif not chase_ok(ev.path[0][1], ev.ref_close, cfg["max_chase"]):
                skipped["already ran >5% (don't chase)"] += 1
                events.append(ev)            # kept for the simulator's later sessions
                ev.ret, ev.spy = {}, {}      # but not graded at the first open
            else:
                events.append(ev)
        if n % 1000 == 0:
            log.info("events: %d / %d tickers", n, len(by_ticker))
    return events, dict(skipped)


def run(conn, root: Path, overrides: dict | None = None, now: datetime | None = None) -> dict:
    cfg = {**DEFAULTS, **(overrides or {})}
    now = now or datetime.now(ET)
    cal, spy = load_calendar(conn)
    if len(cal) < 300:
        raise RuntimeError("no SPY history: run `backtest-load` first")
    signals, sig_skips = load_signals(conn, cfg)
    from .bt_data import FIRST_QUARTER, quarters
    loaded = {r[0] for r in conn.execute("SELECT quarter FROM bt_quarters")}
    data = {"signals": len(signals),
            "quarters": len(loaded),
            "quarters_missing": [q for q in quarters(FIRST_QUARTER, now.date())
                                 if q not in loaded],
            "bars": conn.execute("SELECT count(*) FROM bt_bars").fetchone()[0]}
    reg = ledger.append(conn, "backtest_registered", {
        "strategy": "insider_follow", "config": cfg, "data": data,
        "note": "written before results are computed"})
    log.info("registered backtest config as ledger #%d", reg.seq)
    events, ev_skips = build_events(conn, signals, cal, spy, cfg)
    graded = [e for e in events if e.ret]
    priced = len({(e.sig.ticker, e.sig.filed) for e in events})
    result = {
        "generated_at": now.isoformat(), "config": cfg, "ledger_registration": reg.seq,
        "data": {**data, "events_priced": priced,
                 "coverage": priced / len(signals) if signals else 0,
                 "skipped_signals": sig_skips, "skipped_events": ev_skips,
                 "delisted_or_data_ended": sum(e.delisted for e in events)},
        "event_study": {name: {str(h): study([e for e in graded if f(e)], h)
                               for h in cfg["horizons"]}
                        for name, f in SLICES.items()},
        "by_year_20d": by_year(graded, 20),
    }
    sim = simulate(events, cal, spy, cfg)
    result["portfolio"] = portfolio_stats(sim)
    result["equity_curve"] = [{"day": d.isoformat(), "equity": round(e, 2), "spy": s}
                              for d, e, s in sim.get("curve", [])[::5]]
    p = result["portfolio"]
    res = ledger.append(conn, "backtest_result", {
        "registration_seq": reg.seq,
        "event_study_20d": result["event_study"]["all signals (pre-registered)"]["20"],
        "portfolio": {k: p.get(k) for k in ("cagr", "spy_cagr", "matched_spy_cagr", "sharpe",
                                            "spy_sharpe", "max_drawdown", "p_beats_spy",
                                            "p_beats_matched_spy", "trades")}})
    result["ledger_result"] = res.seq
    write_outputs(result, root, now)
    return result


def write_outputs(result: dict, root: Path, now: datetime) -> list[Path]:
    from .publish import clean
    out_dir = root / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"insider-follow-{now:%Y-%m-%d}.md"
    md.write_text(render_markdown(result), encoding="utf-8")
    js = root / "docs" / "data" / "backtest.json"
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(json.dumps(clean(result), indent=1, sort_keys=True, allow_nan=False) + "\n",
                  encoding="utf-8")
    return [md, js]


def _p(x, d=1, sign=True):
    if x is None:
        return "–"
    return f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%"


def _n(x, d=2):
    return "–" if x is None else f"{x:.{d}f}"


def render_markdown(r: dict) -> str:
    cfg, d, p = r["config"], r["data"], r.get("portfolio") or {}
    pre = r["event_study"]["all signals (pre-registered)"]
    L = [f"# Insider-buy strategy: historical backtest ({r['generated_at'][:10]})", "",
         "Rules follow the live `insider` paper account (slots, sizing, chase limit, stop-loss, "
         "20-session hold, half the slots when SPY is below its 200-day average, the 3% "
         "daily-loss brake and the 15% drawdown halt). Buy at the open of the first session "
         "after the Form 4 filing date; graded against SPY over the same window. Differences "
         "from live trading are listed under Limitations.",
         f"Config registered in the ledger before results were computed (entry "
         f"#{r['ledger_registration']}); results recorded as entry #{r['ledger_result']}.", "",
         "## Data", "",
         f"- Qualifying signals: {d['signals']:,} (officer/director open-market buys "
         f"≥ ${cfg['min_value_usd']:,}, price ≥ ${cfg['min_price']:.0f}, since {cfg['start']})",
         f"- Priced by Alpaca: {d['events_priced']:,} ({_p(d['coverage'], 0, False)} coverage)",
         f"- Skipped: {json.dumps(d['skipped_signals'] | d['skipped_events'])}",
         f"- SEC quarters missing: {', '.join(d.get('quarters_missing') or []) or 'none'}",
         f"- Bars stopped before the exit (delisted or halted): "
         f"{d['delisted_or_data_ended']:,}", "",
         "## Pre-registered test: does the stock beat SPY after an insider buy?", "",
         "| Horizon | Events | Hit rate | Median vs SPY | Capped mean | Beta-adjusted mean "
         "| Months | Month t-stat | Months positive |",
         "|---|---|---|---|---|---|---|---|---|"]
    for h in map(str, cfg["horizons"]):
        s = pre[h]
        if not s.get("n"):
            continue
        L.append(f"| {h} sessions | {s['n']:,} | {_p(s['hit_rate'], 0, False)} | "
                 f"{_p(s['median_excess'], 2)} | {_p(s['capped_mean'], 2)} | "
                 f"{_p(s['mean_abnormal'], 2)} | {s['n_months']} | {_n(s['t_months'], 1)} | "
                 f"{_p(s['pct_months_positive'], 0, False)} |")
    L += ["", "The month t-stat averages each month's events first, so a crowded month "
          "counts once. Above ~2 is meaningful; above ~3 is strong.", "",
          "## Exploratory slices (20 sessions) — not pre-registered", "",
          "| Slice | Events | Hit rate | Median | Capped mean | Month t |",
          "|---|---|---|---|---|---|"]
    for name, hs in r["event_study"].items():
        s = hs.get("20") or {}
        if s.get("n"):
            L.append(f"| {name} | {s['n']:,} | {_p(s['hit_rate'], 0, False)} | "
                     f"{_p(s['median_excess'], 2)} | {_p(s['capped_mean'], 2)} | "
                     f"{_n(s['t_months'], 1)} |")
    L += ["", "Five slices were examined; expect one to look good by luck alone.", "",
          "## By year (20 sessions, all signals)", "",
          "| Year | Events | Hit rate | Median | Capped mean |", "|---|---|---|---|---|"]
    for y, s in r["by_year_20d"].items():
        L.append(f"| {y} | {s['n']:,} | {_p(s['hit_rate'], 0, False)} | "
                 f"{_p(s['median_excess'], 2)} | {_p(s['capped_mean'], 2)} |")
    if p:
        L += ["", f"## Portfolio simulation ({p['start']} to {p['end']}, "
              f"{cfg['cost_bps']} bps costs per side)", "",
              "| | Strategy | SPY buy & hold | SPY at same exposure |", "|---|---|---|---|",
              f"| Annual return | {_p(p['cagr'])} | {_p(p['spy_cagr'])} | "
              f"{_p(p['matched_spy_cagr'])} |",
              f"| Volatility | {_p(p['ann_vol'], 1, False)} | {_p(p['spy_ann_vol'], 1, False)} "
              f"| |",
              f"| Sharpe | {_n(p['sharpe'])} | {_n(p['spy_sharpe'])} | |",
              f"| Max drawdown | {_p(p['max_drawdown'])} | {_p(p['spy_max_drawdown'])} | |",
              "", f"- Average invested: {_p(p['avg_exposure'], 0, False)} "
              f"(the rest sits in cash, earning nothing in this simulation)",
              f"- Trades: {p['trades']:,}; win rate {_p(p['win_rate'], 0, False)}; "
              f"average {_p(p['avg_trade'], 2)}, median {_p(p['median_trade'], 2)}",
              f"- Exits: {json.dumps(p['exits'])}",
              f"- Drawdown halts: {p['drawdown_halts']}; sessions with new entries blocked "
              f"by a safety rule: {p['days_entries_blocked']:,}",
              f"- P(daily returns beat SPY): {_p(p['p_beats_spy'], 0, False)}; "
              f"P(beat SPY at the same exposure): {_p(p['p_beats_matched_spy'], 0, False)}",
              "", "| Year | Strategy | SPY |", "|---|---|---|"]
        for y, v in p["yearly"].items():
            L.append(f"| {y} | {_p(v['strategy'])} | {_p(v['spy'])} |")
    L += ["", "## Limitations (read these before trusting the numbers)", "",
          "- Survivorship: stocks without Alpaca price history drop out; delisted names skew "
          "toward bad outcomes, so results may be optimistic.",
          "- Industry codes and the fund list are today's: a company that was a SPAC when "
          "insiders bought may now be an operating company and slip through.",
          "- Rule 10b5-1 plan flags only exist in filings since 2023 (excluded when present).",
          "- Entry is the next session's open after the filing *date*; live trading often "
          "acts the same day for filings made during market hours, and decides at 9:45 "
          "rather than the open.",
          "- Stop-losses are checked at the open and close only (live: three times a day).",
          "- The $5 minimum uses the insider's reported price, not the market price; the "
          "'don't chase' check compares to the adjusted close on the insider's trade date.",
          "- The 15% drawdown halt is lifted after 5 sessions (live: when you send "
          "/resume confirm).",
          "- Cash earns 0% here; in reality idle cash earns interest.",
          "- The live account also favours stocks with bullish AI news; that can't be "
          "replayed historically.",
          "- Statistics aren't deflated for the 5 slices and 3 live strategies; treat "
          "anything short of a strong pre-registered result as unproven."]
    return "\n".join(L) + "\n"
