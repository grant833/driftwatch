from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from . import db, ledger
from .alerts import alert
from .config import home, load_settings, setup_logging
from .guards import can_trade

log = logging.getLogger("driftwatch")


def cmd_init_db(s, conn, args):
    db.apply_schema(conn)
    log.info("schema applied")


def cmd_news_stream(s, conn, args):
    from .ingest import alpaca_news
    asyncio.run(alpaca_news.stream(s, conn))


def cmd_news_backfill(s, conn, args):
    from .ingest import alpaca_news
    start = datetime.now(UTC) - timedelta(days=args.days)
    n = alpaca_news.backfill(s, conn, start)
    log.info("backfilled %d articles (note: pre-model-cutoff news is for plumbing tests, "
             "not for judging predictive power)", n)


def cmd_edgar_poll(s, conn, args):
    from .ingest import edgar
    edgar.poll(s, conn)


def cmd_gdelt_poll(s, conn, args):
    from .ingest import gdelt
    gdelt.poll(s, conn)


def cmd_insider_poll(s, conn, args):
    from . import insider
    insider.poll(s, conn)


def cmd_analyst(s, conn, args):
    from . import analyst
    analyst.run(s, conn)


def cmd_predictions(s, conn, args):
    rows = conn.execute(
        """
        SELECT p.created_at, p.ticker, p.p_up_mean, p.p_up_std,
               coalesce(p.stance, CASE WHEN p.agree THEN 'agree' ELSE 'split' END),
               p.novelty_mean, p.magnitude, n.headline
        FROM predictions p JOIN news_items n ON n.id = p.news_id
        ORDER BY p.created_at DESC LIMIT %s
        """, (args.last,)).fetchall()
    for r in rows:
        flag = r[4].upper()
        print(f"{r[0]:%m-%d %H:%M}  {r[1]:6s} p_up={r[2]:.2f}±{r[3]:.2f} {flag:7s} "
              f"nov={r[5]:.2f} {r[6]:6s} | {r[7][:70]}")


def cmd_insiders(s, conn, args):
    rows = conn.execute(
        "SELECT filed_at, ticker, insider_name, officer_title, value_usd FROM insider_buy_signals "
        "ORDER BY filed_at DESC NULLS LAST LIMIT %s", (args.last,)).fetchall()
    for r in rows:
        when = f"{r[0]:%m-%d %H:%M}" if r[0] else "?"
        print(f"{when}  {r[1]:6s} ${r[4]:>12,.0f}  {r[2]} ({r[3] or 'director'})")


def cmd_mood(s, conn, args):
    rows = conn.execute(
        "SELECT hour, mood, headlines FROM market_mood_hourly ORDER BY hour DESC LIMIT %s",
        (args.last,)).fetchall()
    for hour, mood, n in rows:
        bar = ("+" * int(max(mood, 0) * 20)) or ("-" * int(max(-mood, 0) * 20))
        print(f"{hour:%m-%d %H:00}  {mood:+.2f}  ({n:3d} headlines)  {bar}")


def cmd_costs(s, conn, args):
    rows = conn.execute(
        "SELECT date_trunc('day', created_at)::date, stage, model, count(*), "
        "sum(input_tokens), sum(output_tokens) FROM llm_calls "
        "GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2 LIMIT 30").fetchall()
    print("day         stage   calls   input_tok  output_tok  model")
    for d, stage, model, n, tin, tout in rows:
        print(f"{d}  {stage:6s} {n:6d} {tin:11,d} {tout:11,d}  {model}")
    print("Multiply token counts by your models' published per-token prices for cost.")


def cmd_ledger_verify(s, conn, args):
    ok, bad, n = ledger.verify(conn)
    if ok:
        log.info("ledger OK: %d entries, chain intact", n)
    else:
        alert(s.slack_webhook, f"LEDGER TAMPERING DETECTED at seq {bad}")
        raise SystemExit(1)


def cmd_ledger_anchor(s, conn, args):
    path = ledger.anchor(conn, home() / "anchors")
    log.info("anchor written: %s (commit and push it)", path) if path else log.info("ledger empty")


def cmd_ledger_note(s, conn, args):
    e = ledger.append(conn, "note", {"text": args.text})
    log.info("appended seq=%d hash=%s", e.seq, e.hash[:16])


def cmd_health(s, conn, args):
    recent = "WHERE received_at > now() - interval '24 hours'"
    day = "WHERE created_at > now() - interval '24 hours'"
    q = {
        "news (24h)": f"SELECT count(*) FROM news_items {recent}",
        "filings (24h)": f"SELECT count(*) FROM filings {recent}",
        "form 4 parsed (24h)": "SELECT count(*) FROM filings WHERE form_type IN ('4','4/A') "
                               "AND parsed_at > now() - interval '24 hours'",
        "insider buys (24h)": f"SELECT count(*) FROM insider_buy_signals {recent}",
        "triaged (24h)": f"SELECT count(*) FROM triage {day}",
        "predictions (24h)": f"SELECT count(*) FROM predictions {day}",
        "ledger entries": "SELECT count(*) FROM ledger",
    }
    stale = []
    for label, sql in q.items():
        n = conn.execute(sql).fetchone()[0]
        print(f"{label:22s} {n}")
        if n == 0 and label in ("news (24h)", "filings (24h)"):
            stale.append(label)
    if stale and args.alert:
        alert(s.slack_webhook, f"no data in: {', '.join(stale)}")


def cmd_check_ticker(s, conn, args):
    funds = frozenset(r[0] for r in conn.execute("SELECT ticker FROM fund_tickers"))
    ok, reason = can_trade(args.ticker, s.blocklist, funds)
    print(f"{args.ticker.upper()}: {'TRADEABLE' if ok else 'BLOCKED'} ({reason})")


def main() -> None:
    setup_logging()
    p = argparse.ArgumentParser(prog="driftwatch")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sub.add_parser("news-stream").set_defaults(fn=cmd_news_stream)
    b = sub.add_parser("news-backfill")
    b.add_argument("--days", type=int, default=7)
    b.set_defaults(fn=cmd_news_backfill)
    sub.add_parser("edgar-poll").set_defaults(fn=cmd_edgar_poll)
    sub.add_parser("gdelt-poll").set_defaults(fn=cmd_gdelt_poll)
    sub.add_parser("insider-poll").set_defaults(fn=cmd_insider_poll)
    sub.add_parser("analyst").set_defaults(fn=cmd_analyst)
    for name, fn, default in (("predictions", cmd_predictions, 20),
                              ("insiders", cmd_insiders, 20),
                              ("mood", cmd_mood, 24)):
        sp = sub.add_parser(name)
        sp.add_argument("--last", type=int, default=default)
        sp.set_defaults(fn=fn)
    sub.add_parser("costs").set_defaults(fn=cmd_costs)
    sub.add_parser("ledger-verify").set_defaults(fn=cmd_ledger_verify)
    sub.add_parser("ledger-anchor").set_defaults(fn=cmd_ledger_anchor)
    n = sub.add_parser("ledger-note")
    n.add_argument("text")
    n.set_defaults(fn=cmd_ledger_note)
    h = sub.add_parser("health")
    h.add_argument("--alert", action="store_true")
    h.set_defaults(fn=cmd_health)
    c = sub.add_parser("check-ticker")
    c.add_argument("ticker")
    c.set_defaults(fn=cmd_check_ticker)

    args = p.parse_args()
    s = load_settings()
    with db.connect(s.database_url) as conn:
        args.fn(s, conn, args)


if __name__ == "__main__":
    main()
