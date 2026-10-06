"""Alpaca (Benzinga) news: real-time WebSocket stream with REST gap-filling.

On every (re)connect we backfill from the newest stored article so outages never
leave silent holes in the dataset.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import websockets

from .. import db
from ..alerts import alert
from ..config import Settings

log = logging.getLogger(__name__)

STREAM_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
REST_URL = "https://data.alpaca.markets/v1beta1/news"
SOURCE = "alpaca_benzinga"


def normalize(msg: dict) -> dict:
    return {
        "source": SOURCE,
        "external_id": str(msg["id"]),
        "headline": msg.get("headline") or "",
        "summary": msg.get("summary") or None,
        "content": msg.get("content") or None,
        "symbols": sorted({s.upper() for s in (msg.get("symbols") or [])}),
        "url": msg.get("url") or None,
        "author": msg.get("author") or None,
        "published_at": msg["created_at"],
        "updated_at": msg.get("updated_at") or None,
        "raw": msg,
    }


def _headers(s: Settings) -> dict:
    return {"APCA-API-KEY-ID": s.alpaca_key, "APCA-API-SECRET-KEY": s.alpaca_secret}


def backfill(s: Settings, conn, start: datetime, end: datetime | None = None) -> int:
    """Page through REST history from `start`. Returns number of new rows."""
    params = {
        "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limit": 50,
        "sort": "asc",
        "include_content": "true",
    }
    if end:
        params["end"] = end.astimezone(UTC).isoformat().replace("+00:00", "Z")
    inserted = 0
    with httpx.Client(headers=_headers(s), timeout=30) as client:
        while True:
            resp = client.get(REST_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            for msg in data.get("news", []):
                inserted += db.insert_news(conn, normalize(msg), via="backfill")
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
    return inserted


def gap_fill(s: Settings, conn) -> None:
    last = db.latest_news_time(conn, SOURCE)
    start = (last - timedelta(minutes=5)) if last else datetime.now(UTC) - timedelta(days=1)
    n = backfill(s, conn, start)
    log.info("gap fill from %s inserted %d", start.isoformat(), n)


async def stream(s: Settings, conn) -> None:
    if not (s.alpaca_key and s.alpaca_secret):
        raise SystemExit("ALPACA_API_KEY / ALPACA_API_SECRET not set")
    backoff = 1
    while True:
        try:
            await asyncio.to_thread(gap_fill, s, conn)
            async with websockets.connect(STREAM_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(
                    {"action": "auth", "key": s.alpaca_key, "secret": s.alpaca_secret}))
                await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                log.info("news stream connected")
                backoff = 1
                async for raw in ws:
                    for msg in json.loads(raw):
                        kind = msg.get("T")
                        if kind == "n":
                            item = normalize(msg)
                            new = await asyncio.to_thread(db.insert_news, conn, item)
                            if new:
                                log.info("news %s %s", item["symbols"], item["headline"][:90])
                        elif kind == "error":
                            raise RuntimeError(f"stream error: {msg}")
                        elif kind in ("success", "subscription"):
                            log.info("control: %s", msg)
        except (OSError, websockets.WebSocketException, RuntimeError, httpx.HTTPError) as exc:
            log.error("news stream dropped: %s (retry in %ss)", exc, backoff)
            if backoff >= 32:
                alert(s.slack_webhook, f"news stream unstable: {exc}", conn=conn)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
