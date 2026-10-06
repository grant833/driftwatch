from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def alert(webhook: str | None, text: str) -> None:
    """Send to Slack if configured; always log."""
    log.warning("ALERT: %s", text)
    if not webhook:
        return
    try:
        httpx.post(webhook, json={"text": f":rotating_light: driftwatch: {text}"}, timeout=10)
    except httpx.HTTPError as exc:
        log.error("Slack alert failed: %s", exc)
