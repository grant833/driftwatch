"""Telegram bot: delivers queued notifications and answers commands from your phone.

Security: the bot only obeys one chat, TELEGRAM_CHAT_ID. Until that is set, it
answers any message with that chat's ID (so you can copy yours into .env) and
does nothing else. Bot usernames are public, so this lock matters.

/kill takes effect immediately. /resume requires "/resume confirm", because
turning trading back on should never happen by accident.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from html import escape

import httpx

from . import reports
from .config import Settings
from .db import get_control, set_control  # noqa: F401 (re-exported)
from .prices import ET

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram's hard limit is 4096
STALE_AFTER = timedelta(hours=24)

HELP = """<b>driftwatch</b>
/status – pipeline health
/today – today's predictions
/insiders – recent insider buys
/score – prediction scorecard vs SPY
/mood – market mood from headlines
/costs – API usage, last 7 days
/positions – open paper positions
/perf – tournament results vs SPY
/kill – halt all trading immediately
/resume confirm – allow trading again
/flatten confirm – sell everything, then halt"""


# ---------------- state helpers ----------------

def set_halt(conn, halted: bool) -> None:
    set_control(conn, "trading_halted", "true" if halted else "false")
    if halted:       # the trader also cancels any orders still working
        set_control(conn, "kill_cancel_done", "false")
    else:            # resuming clears per-account halts and restarts the drawdown peak
        conn.execute("UPDATE controls SET value = 'false', updated_at = now() "
                     "WHERE key LIKE 'halt:%%'")
        set_control(conn, "peak_reset_day", datetime.now(ET).date().isoformat())


def is_halted(conn) -> bool:
    return get_control(conn, "trading_halted") == "true"


def pre(text: str) -> str:
    return f"<pre>{escape(text)}</pre>"


def clip(text: str) -> str:
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 20] + "\n…(truncated)"


# ---------------- commands ----------------

def handle_command(conn, text: str, horizons: list[int], trials: int = 3) -> str:
    parts = text.strip().split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return pre(reports.health(conn)[0])
    if cmd == "/today":
        return pre(reports.predictions(conn, last=25, today_only=True, compact=True))
    if cmd == "/insiders":
        return pre(reports.insiders(conn, last=10))
    if cmd == "/score":
        return pre(reports.score(conn, horizons))
    if cmd == "/mood":
        return pre(reports.mood(conn, last=12))
    if cmd == "/costs":
        return pre(reports.costs(conn, days=7))
    if cmd == "/positions":
        return pre(reports.positions(conn))
    if cmd == "/perf":
        from .perf import report
        return pre(report(conn, trials))
    if cmd == "/flatten":
        if arg != "confirm":
            return ("This sells EVERY position in every paper account and halts trading.\n"
                    "To do it, send exactly: /flatten confirm")
        set_control(conn, "flatten_requested", "true")
        return ("🧯 Flatten requested. Sell orders go out at the next market-open check "
                "(within a minute while the market is open); trading then halts.")
    if cmd == "/kill":
        set_halt(conn, True)
        return ("🛑 <b>Trading HALTED.</b> No new orders will be placed.\n"
                "Send /resume confirm to undo.")
    if cmd == "/resume":
        if arg != "confirm":
            return "To allow trading again, send exactly: /resume confirm"
        set_halt(conn, False)
        return "✅ Trading allowed again."
    return "Unknown command.\n\n" + HELP


def daily_summary(conn, horizons: list[int]) -> str:
    stance = dict(conn.execute(
        "SELECT coalesce(stance, 'split'), count(*) FROM predictions "
        "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'America/New_York') "
        "AT TIME ZONE 'America/New_York' GROUP BY 1").fetchall())
    insider_n = conn.execute(
        "SELECT count(*) FROM insider_buy_signals "
        "WHERE received_at > now() - interval '24 hours'").fetchone()[0]
    calls = dict(conn.execute(
        "SELECT stage, count(*) FROM llm_calls WHERE created_at > now() - interval '24 hours' "
        "GROUP BY 1").fetchall())
    total = sum(stance.values())
    lines = [
        f"📊 <b>driftwatch daily — {datetime.now(ET):%a %b %d}</b>",
        f"Predictions: {total} ({stance.get('agree', 0)} agree, {stance.get('split', 0)} split, "
        f"{stance.get('neutral', 0)} neutral)",
        f"Insider buys (24h): {insider_n}",
        f"API calls (24h): {calls.get('triage', 0)} triage, {calls.get('panel', 0)} panel",
        f"Trading: {'🛑 HALTED' if is_halted(conn) else 'paper (research phase)'}",
    ]
    eq = reports.equity_today(conn)
    if not eq.startswith("No "):
        lines.append("\n<b>Paper accounts</b>")
        lines.append(pre(eq))
    _, stale = reports.health(conn)
    if stale:
        lines.append(f"⚠ No data in: {', '.join(stale)}")
    from .publish import stale as publish_stale
    last = get_control(conn, "last_publish")
    if publish_stale(datetime.now(UTC), last):
        lines.append(f"⚠ Nightly publish hasn't run since {last[:10]}: check the PC's "
                     "scheduled task (ops/nightly.log)")
    if total:
        lines.append("\n<b>Strongest calls today</b>")
        lines.append(pre(reports.predictions(conn, last=5, today_only=True, compact=True,
                                             strongest=True)))
    lines.append("\n<b>Scorecard</b>")
    lines.append(pre(reports.score(conn, horizons)))
    return "\n".join(lines)


def summary_due(now: datetime, at: str, last_sent: str | None) -> bool:
    et = now.astimezone(ET)
    hh, mm = (int(x) for x in at.split(":"))
    return (et.weekday() < 5 and (et.hour, et.minute) >= (hh, mm)
            and last_sent != et.date().isoformat())


# ---------------- Telegram transport ----------------

class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.client = httpx.Client(timeout=40)

    def call(self, method: str, **params) -> dict:
        resp = self.client.post(API.format(token=self.token, method=method), json=params)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(resp.status_code, data.get("description", "unknown error"))
        return data["result"]

    def updates(self, offset: int | None, timeout: int) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def send(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", chat_id=chat_id, text=clip(text), parse_mode="HTML",
                  disable_web_page_preview=True)


class TelegramError(Exception):
    def __init__(self, status: int, description: str):
        super().__init__(f"{status}: {description}")
        self.status = status


def process_updates(conn, tg, owner: int | None, updates: list[dict],
                    horizons: list[int], trials: int = 3) -> int | None:
    """Handle messages; return the next offset."""
    offset = None
    for u in updates:
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = msg.get("text") or ""
        if chat is None:
            continue
        if owner is None:
            tg.send(chat, f"Hi! Your chat ID is <code>{chat}</code>.\nAdd this line to your "
                          f".env file, then restart the notifier:\n"
                          f"<code>TELEGRAM_CHAT_ID={chat}</code>")
            continue
        if chat != owner:
            log.warning("ignoring message from unknown chat %s", chat)
            continue
        try:
            tg.send(owner, handle_command(conn, text, horizons, trials))
        except TelegramError as exc:
            log.error("reply failed: %s", exc)
    if offset is not None:
        set_control(conn, "telegram_offset", str(offset))
    return offset


def flush_outbox(conn, tg, owner: int, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT id, text, created_at FROM notifications WHERE sent_at IS NULL "
        "ORDER BY id LIMIT 20").fetchall()
    sent = 0
    for nid, text, created in rows:
        if now - created <= STALE_AFTER:
            try:
                tg.send(owner, text)
                sent += 1
            except TelegramError as exc:
                if exc.status != 400:  # transient: leave it queued and retry later
                    raise
                log.error("dropping undeliverable notification %s: %s", nid, exc)
        conn.execute("UPDATE notifications SET sent_at = now() WHERE id = %s", (nid,))
        time.sleep(0.05)
    return sent


def run(s: Settings, conn, tg=None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    owner = int(chat) if chat.lstrip("-").isdigit() else None
    cfg = s.raw.get("notifier", {})
    horizons = s.raw["scorer"]["horizons"]
    if not token and tg is None:
        log.error("TELEGRAM_BOT_TOKEN not set; notifier idle")
        while True:
            time.sleep(600)
    tg = tg or Telegram(token)
    if owner is None:
        log.warning("TELEGRAM_CHAT_ID not set: message your bot to get your chat ID")
    saved = get_control(conn, "telegram_offset")
    offset = int(saved) if saved else None
    while True:
        try:
            pending = owner is not None and conn.execute(
                "SELECT 1 FROM notifications WHERE sent_at IS NULL LIMIT 1").fetchone()
            updates = tg.updates(offset, timeout=2 if pending else 25)
            offset = process_updates(conn, tg, owner, updates, horizons,
                                     s.raw.get("trading", {}).get("trials_count", 3)) or offset
            if owner is not None:
                flush_outbox(conn, tg, owner)
                now = datetime.now(UTC)
                if summary_due(now, cfg.get("daily_summary_time", "16:45"),
                               get_control(conn, "last_summary")):
                    tg.send(owner, daily_summary(conn, horizons))
                    set_control(conn, "last_summary", now.astimezone(ET).date().isoformat())
        except (httpx.HTTPError, TelegramError) as exc:
            log.error("telegram error: %s", exc)
            time.sleep(10)
        except Exception as exc:  # keep the bot alive
            log.exception("notifier cycle failed: %s", exc)
            time.sleep(10)
