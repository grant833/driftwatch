"""GDELT global news tone. Used as a market-wide risk dial, never to pick stocks.

GDELT's free API throttles aggressively, so every request retries with backoff
and honors Retry-After when present.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx

from .. import db
from ..alerts import alert
from ..config import Settings

log = logging.getLogger(__name__)

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
RETRY_WAITS = (10, 30, 60, 120)
RETRYABLE = {429, 500, 502, 503, 504}


def parse_timeline(data: dict) -> list[tuple[datetime, float]]:
    points = []
    for series in data.get("timeline", []):
        for p in series.get("data", []):
            dt = datetime.strptime(p["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            points.append((dt, float(p["value"])))
    return points


def _retry_after(resp: httpx.Response, default: int) -> int:
    try:
        return max(default, int(resp.headers.get("Retry-After", "")))
    except ValueError:
        return default


def fetch_tone(client: httpx.Client, query: str, timespan: str) -> dict:
    params = {"query": query, "mode": "timelinetone", "format": "json", "timespan": timespan}
    for attempt, wait in enumerate((*RETRY_WAITS, None), start=1):
        try:
            resp = client.get(DOC_API, params=params)
        except httpx.TransportError as exc:  # DNS hiccups, dropped connections
            if wait is None:
                raise
            log.warning("GDELT network error (attempt %d): %s; retrying in %ss",
                        attempt, exc, wait)
            time.sleep(wait)
            continue
        if resp.status_code not in RETRYABLE or wait is None:
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError as exc:
                raise ValueError(f"non-JSON reply: {resp.text[:300]!r}") from exc
        wait = _retry_after(resp, wait)
        log.warning("GDELT returned %s (attempt %d); retrying in %ss",
                    resp.status_code, attempt, wait)
        time.sleep(wait)
    raise RuntimeError("unreachable")


def poll(s: Settings, conn) -> None:
    cfg = s.gdelt
    failures = 0
    headers = {"User-Agent": f"driftwatch/0.1 research ({s.sec_user_agent})"}
    with httpx.Client(timeout=60, headers=headers) as client:
        while True:
            for name, query in cfg["queries"].items():
                try:
                    data = fetch_tone(client, query, cfg["timespan"])
                    points = parse_timeline(data)
                    if not points:
                        log.warning("GDELT %s returned no data points; reply was: %s",
                                    name, str(data)[:300])
                    n = db.insert_tone(conn, name, points)
                    log.info("GDELT %s: %d new buckets", name, n)
                    failures = 0
                except (httpx.HTTPError, ValueError) as exc:
                    failures += 1
                    log.error("GDELT %s failed after retries: %s", name, exc)
                    if failures == 6:
                        alert(s.slack_webhook, f"GDELT poller failing repeatedly: {exc}")
                time.sleep(cfg["spacing_seconds"])
            time.sleep(cfg["poll_seconds"])
