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
    q = {
        "news (24h)": f"SELECT count(*) FROM news_items {recent}",
        "filings (24h)": f"SELECT count(*) FROM filings {recent}",
        "gdelt buckets (24h)": f"SELECT count(*) FROM gdelt_tone {recent}",
        "ledger entries": "SELECT count(*) FROM ledger",
    }
    stale = []
    for label, sql in q.items():
        n = conn.execute(sql).fetchone()[0]
        print(f"{label:22s} {n}")
        if n == 0 and "24h" in label:
            stale.append(label)
    if stale and args.alert:
        alert(s.slack_webhook, f"no data in: {', '.join(stale)}")


def cmd_check_ticker(s, conn, args):
    ok, reason = can_trade(args.ticker, s.blocklist)
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
