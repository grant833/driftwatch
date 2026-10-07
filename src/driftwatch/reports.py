"""Plain-text reports shared by the command line and the Telegram bot."""
from __future__ import annotations

from .scorer import scorecard

RECENT = "WHERE received_at > now() - interval '24 hours'"
DAY = "WHERE created_at > now() - interval '24 hours'"

HEALTH_QUERIES = {
    "news (24h)": f"SELECT count(*) FROM news_items {RECENT}",
    "filings (24h)": f"SELECT count(*) FROM filings {RECENT}",
    "form 4 parsed (24h)": "SELECT count(*) FROM filings WHERE form_type IN ('4','4/A') "
                           "AND parsed_at > now() - interval '24 hours'",
    "insider buys (24h)": f"SELECT count(*) FROM insider_buy_signals {RECENT}",
    "triaged (24h)": f"SELECT count(*) FROM triage {DAY}",
    "predictions (24h)": f"SELECT count(*) FROM predictions {DAY}",
    "outcomes scored": "SELECT count(*) FROM outcomes",
    "ledger entries": "SELECT count(*) FROM ledger",
}


def health(conn) -> tuple[str, list[str]]:
    lines, stale = [], []
    for label, sql in HEALTH_QUERIES.items():
        n = conn.execute(sql).fetchone()[0]
        lines.append(f"{label:20s} {n}")
        if n == 0 and label in ("news (24h)", "filings (24h)"):
            stale.append(label)
    halted = conn.execute(
        "SELECT value FROM controls WHERE key = 'trading_halted'").fetchone()
    lines.append(f"{'trading halted':20s} {'YES' if halted and halted[0] == 'true' else 'no'}")
    return "\n".join(lines), stale


def predictions(conn, last: int = 20, today_only: bool = False, compact: bool = False,
                strongest: bool = False) -> str:
    """Recent predictions. strongest=True ranks by conviction (distance from 0.5) and
    leaves out neutral calls."""
    conds = []
    if today_only:
        conds.append("p.created_at >= date_trunc('day', now() AT TIME ZONE 'America/New_York') "
                     "AT TIME ZONE 'America/New_York'")
    if strongest:
        conds.append("coalesce(p.stance, 'split') <> 'neutral'")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    order = "abs(p.p_up_mean - 0.5) DESC, p.created_at DESC" if strongest else "p.created_at DESC"
    rows = conn.execute(
        f"""
        SELECT p.created_at, p.ticker, p.p_up_mean, p.p_up_std,
               coalesce(p.stance, CASE WHEN p.agree THEN 'agree' ELSE 'split' END),
               p.novelty_mean, p.magnitude, n.headline, p.pre_move
        FROM predictions p JOIN news_items n ON n.id = p.news_id
        {where}
        ORDER BY {order} LIMIT %s
        """, (last,)).fetchall()
    if not rows:
        return "No predictions yet."
    out = []
    for r in rows:
        move = f" {r[8]:+.1%}" if r[8] is not None else ""
        if compact:
            out.append(f"{r[1]:5s} {r[2]:.2f} {r[4][:5].upper():5s}{move} | {r[7][:38]}")
        else:
            out.append(f"{r[0]:%m-%d %H:%M}  {r[1]:6s} p_up={r[2]:.2f}±{r[3]:.2f} "
                       f"{r[4].upper():7s} nov={r[5]:.2f} {r[6]:6s}{move} | {r[7][:70]}")
    return "\n".join(out)


def insiders(conn, last: int = 20) -> str:
    rows = conn.execute(
        "SELECT filed_at, ticker, insider_name, officer_title, value_usd FROM insider_buy_signals "
        "ORDER BY filed_at DESC NULLS LAST LIMIT %s", (last,)).fetchall()
    if not rows:
        return "No insider buy signals yet."
    return "\n".join(
        f"{(r[0].strftime('%m-%d %H:%M') if r[0] else '?')}  {r[1]:6s} ${r[4]:>12,.0f}  "
        f"{r[2]} ({r[3] or 'director'})" for r in rows)


def mood(conn, last: int = 24) -> str:
    rows = conn.execute(
        "SELECT hour, mood, headlines FROM market_mood_hourly ORDER BY hour DESC LIMIT %s",
        (last,)).fetchall()
    if not rows:
        return "No mood data yet."
    out = []
    for hour, m, n in rows:
        bar = ("+" * int(max(m, 0) * 20)) or ("-" * int(max(-m, 0) * 20))
        out.append(f"{hour:%m-%d %H:00} {m:+.2f} ({n:3d}) {bar}")
    return "\n".join(out)


def costs(conn, days: int = 7) -> str:
    rows = conn.execute(
        "SELECT date_trunc('day', created_at)::date, stage, count(*), "
        "sum(input_tokens), sum(output_tokens) FROM llm_calls "
        "WHERE created_at > now() - make_interval(days => %s) "
        "GROUP BY 1, 2 ORDER BY 1 DESC, 2", (days,)).fetchall()
    if not rows:
        return "No API calls recorded yet."
    out = ["day        stage  calls   in_tok  out_tok"]
    for d, stage, n, tin, tout in rows:
        out.append(f"{d} {stage:6s} {n:5d} {tin:8,d} {tout:8,d}")
    out.append("Exact $ spend: console.anthropic.com > Usage")
    return "\n".join(out)


def score(conn, horizons: list[int]) -> str:
    card = scorecard(conn, horizons)
    out = []
    for h, c in card.items():
        out.append(f"── {h}-day vs SPY ──")
        if not c.get("n_dir"):
            out.append(f"  {c['n']} scored, none directional yet")
            continue
        t = c.get("t_stat")
        ic = c.get("ic")
        out.append(f"  scored {c['n']} | leaning {c['n_dir']}")
        out.append(f"  hit rate   {c['hit_rate']:.0%}")
        out.append(f"  avg edge   {c['mean_signed']:+.2%}"
                   + (f"  (t={t:+.1f})" if t is not None else ""))
        if ic is not None:
            out.append(f"  IC         {ic:+.2f}")
        for st in ("agree", "split"):
            if st in c:
                s = c[st]
                out.append(f"  {st:6s} n={s['n']:<4d} hit {s['hit_rate']:.0%} "
                           f"edge {s['mean_signed']:+.2%}")
    smallest = min((c.get("n_dir", 0) for c in card.values()), default=0)
    if smallest < 100:
        out.append("\n⚠ Under ~100 scored calls per horizon this is mostly noise.")
    return "\n".join(out)
