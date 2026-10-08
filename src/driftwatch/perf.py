"""Honest performance statistics for the paper tournament.

Each account is compared with simply buying and holding SPY over the same days.
Sharpe ratios are tested with the Probabilistic Sharpe Ratio (Bailey & Lopez de Prado):
the probability the true Sharpe beats a benchmark, given sample length, skew and fat tails.
The Deflated Sharpe Ratio raises that benchmark to the best Sharpe you'd expect from luck
alone after trying `trials` strategies, which is the correct bar when picking a winner.
"""
from __future__ import annotations

import math
import statistics
from statistics import NormalDist

EULER = 0.5772156649
N = NormalDist()


BETA_MIN = 40   # fewest daily returns we'll estimate a beta from


def beta_of(stock: list[float], market: list[float]) -> float | None:
    rs = [b / a - 1 for a, b in zip(stock, stock[1:], strict=False) if a > 0]
    rm = [b / a - 1 for a, b in zip(market, market[1:], strict=False) if a > 0]
    if len(rs) != len(rm) or len(rs) < BETA_MIN:
        return None
    var = statistics.pvariance(rm)
    if var <= 0:
        return None
    mr, ms = statistics.fmean(rm), statistics.fmean(rs)
    return sum((a - ms) * (b - mr) for a, b in zip(rs, rm, strict=True)) / len(rs) / var


def returns(values: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(values, values[1:], strict=False) if a > 0]


def max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return worst


def moments(r: list[float]) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis; a normal distribution gives (0, 3)."""
    m = statistics.fmean(r)
    s = statistics.pstdev(r)
    if s == 0:
        return 0.0, 3.0
    skew = statistics.fmean([((x - m) / s) ** 3 for x in r])
    kurt = statistics.fmean([((x - m) / s) ** 4 for x in r])
    return skew, kurt


def sharpe(r: list[float]) -> float | None:
    """Per-period (daily) Sharpe, no risk-free rate."""
    if len(r) < 2:
        return None
    sd = statistics.stdev(r)
    return statistics.fmean(r) / sd if sd > 0 else None


def psr(r: list[float], sr_star: float = 0.0) -> float | None:
    sr = sharpe(r)
    if sr is None or len(r) < 3:
        return None
    skew, kurt = moments(r)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    return N.cdf((sr - sr_star) * math.sqrt(len(r) - 1) / math.sqrt(denom))


def expected_max_sharpe(sr_variance: float, trials: int) -> float:
    if trials < 2 or sr_variance <= 0:
        return 0.0
    return math.sqrt(sr_variance) * ((1 - EULER) * N.inv_cdf(1 - 1 / trials)
                                     + EULER * N.inv_cdf(1 - 1 / (trials * math.e)))


def account_stats(eq: list[float], spy: list[float]) -> dict:
    r, b = returns(eq), returns(spy)
    out = {"days": len(eq), "ret": eq[-1] / eq[0] - 1 if eq and eq[0] else 0.0,
           "spy_ret": spy[-1] / spy[0] - 1 if spy and spy[0] else 0.0,
           "max_dd": max_drawdown(eq), "r": r}
    out["excess"] = out["ret"] - out["spy_ret"]
    sr = sharpe(r)
    out["sharpe_ann"] = sr * math.sqrt(252) if sr is not None else None
    sb = sharpe(b)
    out["spy_sharpe_ann"] = sb * math.sqrt(252) if sb is not None else None
    out["psr"] = psr(r)
    return out


def tournament(conn, trials: int) -> tuple[dict, list[str], float]:
    """Per-account stats (with 'dsr' added), accounts still waiting, and the luck bar."""
    accts = [r[0] for r in conn.execute("SELECT DISTINCT account FROM equity_daily ORDER BY 1")]
    stats, waiting = {}, []
    for a in accts:
        rows = conn.execute("SELECT equity, spy_close FROM equity_daily WHERE account = %s "
                            "AND equity > 0 AND spy_close > 0 ORDER BY day", (a,)).fetchall()
        if len(rows) < 2:
            waiting.append(a)
            continue
        stats[a] = account_stats([r[0] for r in rows], [r[1] for r in rows])
    srs = [sharpe(s["r"]) for s in stats.values() if sharpe(s["r"]) is not None]
    sr0 = expected_max_sharpe(statistics.pvariance(srs) if len(srs) > 1 else 0.0, trials)
    for s in stats.values():
        s["dsr"] = psr(s["r"], sr0) if sr0 > 0 else None
    return stats, waiting, sr0


def report(conn, trials: int) -> str:
    stats, waiting, sr0 = tournament(conn, trials)
    if not stats and not waiting:
        return "No end-of-day equity recorded yet (first snapshot after 4:10pm ET)."
    if not stats:
        return "Need at least two end-of-day snapshots before showing results."
    lines = []
    for a, s in stats.items():
        lines.append(f"── {a} ({s['days']} days) ──")
        lines.append(f"  return  {s['ret']:+.2%}  vs SPY {s['spy_ret']:+.2%}  "
                     f"(excess {s['excess']:+.2%})")
        lines.append(f"  max drawdown {s['max_dd']:.1%}")
        if s["sharpe_ann"] is not None:
            sp = f"{s['spy_sharpe_ann']:.2f}" if s["spy_sharpe_ann"] is not None else "n/a"
            lines.append(f"  Sharpe {s['sharpe_ann']:.2f}  (SPY {sp})")
        if s["psr"] is not None:
            lines.append(f"  P(real Sharpe > 0)        {s['psr']:.0%}")
            if s["dsr"] is not None:
                lines.append(f"  P(beats luck, {trials} trials)  {s['dsr']:.0%}")
    for a in waiting:
        lines.append(f"── {a} ── first day recorded; results start after day 2")
    lines.append(f"\nGo/no-go needs ~60+ trading days. Strategies tried so far: {trials}.")
    return "\n".join(lines)
