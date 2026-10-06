"""Alerts and notifications.

notify() queues a message in the notifications table (the "outbox"); the notifier
service delivers queued messages to Telegram. Queuing in the database means a
message is never lost if Telegram or the notifier is briefly down.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def notify(conn, kind: str, text: str) -> None:
    try:
        conn.execute("INSERT INTO notifications (kind, text) VALUES (%s, %s)", (kind, text))
    except Exception as exc:  # never let a notification break the caller
        log.error("could not queue notification: %s", exc)


def alert(webhook: str | None, text: str, conn=None) -> None:
    """Problem alert: always logged; sent to Telegram (via outbox) and Slack if configured."""
    log.warning("ALERT: %s", text)
    if conn is not None:
        notify(conn, "alert", f"🚨 {text}")
    if not webhook:
        return
    try:
        httpx.post(webhook, json={"text": f":rotating_light: driftwatch: {text}"}, timeout=10)
    except httpx.HTTPError as exc:
        log.error("Slack alert failed: %s", exc)
