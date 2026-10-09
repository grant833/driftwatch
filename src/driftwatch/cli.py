from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from . import db, ledger, reports
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
    print(reports.predictions(conn, args.last))


def cmd_insiders(s, conn, args):
    print(reports.insiders(conn, args.last))


def cmd_mood(s, conn, args):
    print(reports.mood(conn, args.last))


def cmd_costs(s, conn, args):
    print(reports.costs(conn, days=30))


def cmd_score(s, conn, args):
    print(reports.score(conn, s.raw["scorer"]["horizons"]))


def cmd_scorer(s, conn, args):
    from . import scorer
    scorer.run(s, conn)


def cmd_notifier(s, conn, args):
    from . import notifier
    notifier.run(s, conn)


def cmd_trader(s, conn, args):
    from . import trader
    trader.run(s, conn)


def cmd_positions(s, conn, args):
    print(reports.positions(conn))


def cmd_perf(s, conn, args):
    from .perf import report
    print(report(conn, s.raw["trading"].get("trials_count", 3)))


def cmd_kill(s, conn, args):
    from .notifier import set_halt
    set_halt(conn, True)
    print("trading HALTED")


def cmd_resume(s, conn, args):
    from .notifier import set_halt
    set_halt(conn, False)
    print("trading resumed")


def cmd_ledger_verify(s, conn, args):
    ok, bad, n = ledger.verify(conn)
    if ok:
        log.info("ledger OK: %d entries, chain intact", n)
    else:
        alert(s.slack_webhook, f"LEDGER TAMPERING DETECTED at seq {bad}", conn=conn)
        raise SystemExit(1)


def cmd_ledger_anchor(s, conn, args):
    from .prices import ET
    path = ledger.anchor(conn, home() / "anchors", today=datetime.now(ET).date())
    log.info("anchor written: %s (commit and push it)", path) if path else log.info("ledger empty")


def cmd_publish(s, conn, args):
    from . import publish
    try:
        paths = publish.run(conn, home(), s.raw["scorer"]["horizons"],
                            s.raw.get("trading", {}).get("trials_count", 3))
    except RuntimeError as exc:
        log.error("publish refused: %s", exc)
        raise SystemExit(1) from exc
    for p in paths:
        log.info("wrote %s", p.relative_to(home()))


def cmd_equity_start(s, conn, args):
    from .trader import start_rows
    rows = start_rows(conn, args.equity)
    for acct, day, eq, spy in rows:
        log.info("%s: start row %s equity=%.2f SPY=%.2f", acct, day, eq, spy)
    if not rows:
        log.info("nothing to add")


def cmd_backtest_load(s, conn, args):
    """Download history for backtests (SEC insider data, industry codes, Alpaca bars)."""
    from . import bt_data
    from .trader import sec_lookup
    only = args.quarters.split(",") if args.quarters else None
    n = bt_data.load_sec(conn, s.sec_user_agent, only=only)
    log.info("SEC: %d new open-market purchases loaded", n)
    n = bt_data.load_sic(conn, sec_lookup(s.sec_user_agent))
    log.info("SEC industry codes: %d new issuers", n)
    n = bt_data.load_bars(conn, bt_data.BarClient(s.alpaca_key, s.alpaca_secret))
    log.info("bars: %d new rows. Next: backtest-insider", n)


def cmd_backtest_insider(s, conn, args):
    from . import backtest
    r = backtest.run(conn, home())
    pre = r["event_study"]["all signals (pre-registered)"].get("20", {})
    p = r.get("portfolio") or {}
    print(f"signals {r['data']['signals']:,}, priced {r['data']['events_priced']:,}")
    if pre.get("n"):
        print(f"20-session vs SPY: median {pre['median_excess']:+.2%}, capped mean "
              f"{pre['capped_mean']:+.2%}, hit {pre['hit_rate']:.0%}, month t "
              f"{pre['t_months'] or 0:+.1f}")
    if p:
        print(f"portfolio: {p['cagr']:+.1%}/yr vs SPY {p['spy_cagr']:+.1%} "
              f"(same exposure {p['matched_spy_cagr']:+.1%}), Sharpe {p['sharpe'] or 0:.2f}, "
              f"max drawdown {p['max_drawdown']:.1%}")
    print("full report: backtests/ folder and the dashboard")


def cmd_backtest_diagnose(s, conn, args):
    from . import bt_diagnose
    out = bt_diagnose.run(conn, home())
    print(open(out["path"], encoding="utf-8").read())


def cmd_experiments(s, conn, args):
    from .experiments import report
    print(report(conn, s.raw["scorer"]["horizons"]))


def cmd_notify(s, conn, args):
    from .alerts import notify
    notify(conn, "alert", f"🚨 {args.text}")
    log.info("queued for Telegram")


def cmd_ledger_note(s, conn, args):
    e = ledger.append(conn, "note", {"text": args.text})
    log.info("appended seq=%d hash=%s", e.seq, e.hash[:16])


def cmd_health(s, conn, args):
    text, stale = reports.health(conn)
    print(text)
    if stale and args.alert:
        alert(s.slack_webhook, f"no data in: {', '.join(stale)}", conn=conn)


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
    sub.add_parser("score").set_defaults(fn=cmd_score)
    sub.add_parser("scorer").set_defaults(fn=cmd_scorer)
    sub.add_parser("notifier").set_defaults(fn=cmd_notifier)
    sub.add_parser("kill").set_defaults(fn=cmd_kill)
    sub.add_parser("trader").set_defaults(fn=cmd_trader)
    sub.add_parser("positions").set_defaults(fn=cmd_positions)
    sub.add_parser("perf").set_defaults(fn=cmd_perf)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    sub.add_parser("ledger-verify").set_defaults(fn=cmd_ledger_verify)
    sub.add_parser("ledger-anchor").set_defaults(fn=cmd_ledger_anchor)
    sub.add_parser("publish").set_defaults(fn=cmd_publish)
    es = sub.add_parser("equity-start")
    es.add_argument("--equity", type=float, default=100000.0)
    es.set_defaults(fn=cmd_equity_start)
    bl = sub.add_parser("backtest-load")
    bl.add_argument("--quarters", help="comma list like 2024q1,2024q2 (default: all)")
    bl.set_defaults(fn=cmd_backtest_load)
    sub.add_parser("backtest-insider").set_defaults(fn=cmd_backtest_insider)
    sub.add_parser("backtest-diagnose").set_defaults(fn=cmd_backtest_diagnose)
    sub.add_parser("experiments").set_defaults(fn=cmd_experiments)
    nt = sub.add_parser("notify")
    nt.add_argument("text")
    nt.set_defaults(fn=cmd_notify)
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
