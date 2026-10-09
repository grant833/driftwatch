"""Pre-registered questions about the AI panel, answered from its own live predictions.

Each question comes from published research (see backtests/RESEARCH.md) and was written
down before any answer existed. They are checked on every graded call, so nothing extra is
traded and no extra paper accounts are needed:

  Q1 Horizon: is the edge mostly in the first 1-2 sessions (as studies of LLM news
     signals find), with days 3-5 adding noise?
  Q2 Hard vs soft news: do calls on quantified news (earnings, guidance, analyst ratings,
     M&A) beat calls on soft news (products, management, other)?
  Q3 Reaction agreement: do calls that agree with the stock's early price reaction beat
     calls that fight it? (Research on underreaction says follow the reaction; the live
     strategy currently avoids stocks that already moved a lot.)
  Q4 Calibration: when the panel says 60%, is it right ~60% of the time?
  Q5 Fast insider entry: insider buys priced from the moment the bot saw the filing
     (market hours only), vs small caps. Measured, never traded (see fast_insider.py).

A difference only counts once it is large relative to its by-day noise (|t| > 2) and the
answers will be judged in mid-November 2026 (~25 trading days).
"""
from __future__ import annotations

from .scorer import summarize

HARD = ("earnings", "guidance", "analyst_rating", "m_and_a")

QUESTIONS = {
    "Q2 hard news": "t.category = ANY(%(hard)s)",
    "Q2 soft news": "NOT (t.category = ANY(%(hard)s))",
    "Q3 agrees with reaction": "p.pre_move IS NOT NULL AND abs(p.pre_move) >= 0.005 AND "
                               "sign(p.pre_move) = sign(p.p_up_mean - 0.5)",
    "Q3 fights reaction": "p.pre_move IS NOT NULL AND abs(p.pre_move) >= 0.005 AND "
                          "sign(p.pre_move) = -sign(p.p_up_mean - 0.5)",
}


def _rows(conn, h: int, where: str = "TRUE") -> list[tuple]:
    return conn.execute(
        "SELECT p.p_up_mean, o.excess, "
        "       coalesce(p.stance, CASE WHEN p.agree THEN 'agree' ELSE 'split' END), "
        "       o.entry_day "
        "FROM outcomes o JOIN predictions p USING (news_id, ticker) "
        "LEFT JOIN triage t ON t.news_id = p.news_id "
        f"WHERE o.horizon = %(h)s AND {where}", {"h": h, "hard": list(HARD)}).fetchall()


def _line(label: str, c: dict) -> str:
    if not c.get("n_dir"):
        return f"  {label:26s} no directional calls yet"
    td = c.get("t_days")
    return (f"  {label:26s} n={c['n_dir']:<4d} hit {c['hit_rate']:.0%}  "
            f"med {c['median_signed']:+.2%}  "
            f"by-day t {'–' if td is None else f'{td:+.1f}'}")


def calibration(conn, h: int = 5) -> list[tuple[str, int, float | None]]:
    """(bucket, n, share that beat SPY) for the panel's stated P(up)."""
    out = []
    for lo, hi in ((0.0, 0.45), (0.45, 0.5), (0.5, 0.55), (0.55, 0.6), (0.6, 1.01)):
        r = conn.execute(
            "SELECT count(*), avg((o.excess > 0)::int) FROM outcomes o "
            "JOIN predictions p USING (news_id, ticker) WHERE o.horizon = %s "
            "AND p.p_up_mean >= %s AND p.p_up_mean < %s", (h, lo, hi)).fetchone()
        out.append((f"{lo:.2f}-{min(hi, 1):.2f}", r[0], r[1]))
    return out


def report(conn, horizons: list[int]) -> str:
    lines = ["Pre-registered questions (judge in mid-November; |by-day t| > 2 counts)", "",
             "Q1 Which horizon holds the edge? (all directional calls)"]
    for h in horizons:
        lines.append(_line(f"{h} session(s)", summarize(_rows(conn, h))))
    h = 5 if 5 in horizons else horizons[-1]
    for q in ("Q2", "Q3"):
        lines += ["", f"{q} at {h} sessions:"]
        for name, where in QUESTIONS.items():
            if name.startswith(q):
                lines.append(_line(name[3:], summarize(_rows(conn, h, where))))
    lines += ["", f"Q4 Calibration at {h} sessions (stated P(up) vs share that beat SPY):"]
    for bucket, n, share in calibration(conn, h):
        lines.append(f"  P(up) {bucket}: n={n:<4d} beat SPY "
                     f"{'–' if share is None else f'{share:.0%}'}")
    from .fast_insider import report_lines
    lines += [""] + report_lines(conn)
    return "\n".join(lines)
