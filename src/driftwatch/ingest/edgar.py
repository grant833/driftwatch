"""SEC EDGAR current-filings poller (8-K material events, Form 4 insider trades).

Uses the public Atom feed of the latest filings. Stays far below SEC's
10 requests/second fair-access limit. Form 4 XML parsing (transaction code P)
arrives in Phase 1; Phase 0 stores filing metadata and links.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import feedparser
import httpx

from .. import db
from ..alerts import alert
from ..config import Settings

log = logging.getLogger(__name__)

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)$")
ACCESSION_RE = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")
ITEM_RE = re.compile(r"Item (\d+\.\d+)")

# For filings listed under several parties (Form 4 shows issuer and reporting owner),
# prefer the company's entry.
ROLE_PRIORITY = {"Issuer": 0, "Filer": 1, "Subject": 2, "Reporting": 3}


def parse_feed(xml: str, ticker_map: dict[str, str]) -> list[dict]:
    parsed = feedparser.parse(xml)
    best: dict[str, dict] = {}
    for e in parsed.entries:
        m = TITLE_RE.match(e.get("title", "").strip())
        acc = ACCESSION_RE.search(e.get("id", "") or "")
        if not (m and acc):
            continue
        cik = m["cik"]
        filing = {
            "accession": acc.group(1),
            "form_type": m["form"].strip(),
            "cik": cik,
            "ticker": ticker_map.get(cik),
            "company": m["company"].strip(),
            "role": m["role"],
            "items": sorted(set(ITEM_RE.findall(e.get("summary", "") or ""))),
            "filed_at": datetime.fromisoformat(e["updated"]) if e.get("updated") else None,
            "url": e.get("link", ""),
            "raw": {"title": e.get("title"), "summary": e.get("summary"), "id": e.get("id")},
        }
        cur = best.get(filing["accession"])
        if cur is None or ROLE_PRIORITY.get(filing["role"], 9) < ROLE_PRIORITY.get(cur["role"], 9):
            best[filing["accession"]] = filing
    return list(best.values())


def parse_ticker_map(data: dict) -> dict[str, str]:
    return {str(v["cik_str"]).zfill(10): v["ticker"].upper() for v in data.values()}


def poll(s: Settings, conn) -> None:
    cfg = s.edgar
    client = httpx.Client(headers={"User-Agent": s.sec_user_agent}, timeout=30)
    ticker_map: dict[str, str] = {}
    map_loaded_at = 0.0
    failures = 0
    while True:
        try:
            if time.time() - map_loaded_at > cfg["ticker_map_refresh_hours"] * 3600:
                ticker_map = parse_ticker_map(client.get(TICKERS_URL).raise_for_status().json())
                map_loaded_at = time.time()
                log.info("loaded %d CIK->ticker mappings", len(ticker_map))
            for form in cfg["forms"]:
                resp = client.get(FEED_URL, params={
                    "action": "getcurrent", "type": form, "owner": "include",
                    "count": 100, "output": "atom",
                })
                resp.raise_for_status()
                new = sum(db.insert_filing(conn, f) for f in parse_feed(resp.text, ticker_map))
                log.info("EDGAR %s: %d new", form, new)
                time.sleep(1)
            failures = 0
        except httpx.HTTPError as exc:
            failures += 1
            log.error("EDGAR poll failed: %s", exc)
            if failures == 5:
                alert(s.slack_webhook, f"EDGAR poller failing repeatedly: {exc}")
        time.sleep(cfg["poll_seconds"])
