"""Alpaca market data: live snapshots (for the "already priced in?" check) and
split/dividend-adjusted daily bars (for scoring predictions).

Free-plan notes: snapshots use the IEX feed (real time, but only ~2-3% of volume,
so thin stocks can show stale prices). Historical SIP bars are free once they are
more than 15 minutes old, which is all the scorekeeper needs.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2/stocks"
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)


class PriceSource(Protocol):
    def snapshots(self, tickers: list[str]) -> dict[str, dict]: ...
    def daily_bars(self, tickers: list[str], start: date, end: date) -> list[tuple]: ...


def parse_snapshots(data: dict) -> dict[str, dict]:
    """-> {ticker: {"price", "prev_close", "trade_time"}}; tolerates a wrapped layout."""
    if isinstance(data.get("snapshots"), dict):
        data = data["snapshots"]
    out = {}
    for sym, snap in data.items():
        if not isinstance(snap, dict):
            continue
        trade = snap.get("latestTrade") or {}
        prev = snap.get("prevDailyBar") or {}
        if trade.get("p") and prev.get("c"):
            out[sym.upper()] = {
                "price": float(trade["p"]),
                "prev_close": float(prev["c"]),
                "trade_time": trade.get("t"),
            }
    return out


def bar_day(ts: str) -> date:
    """Daily bar timestamps mark the session start; convert to the Eastern trading date."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(ET).date()


def parse_bars(data: dict) -> list[tuple]:
    rows = []
    for sym, bars in (data.get("bars") or {}).items():
        for b in bars or []:
            rows.append((sym.upper(), bar_day(b["t"]), float(b["o"]), float(b["c"]),
                         float(b.get("v") or 0)))
    return rows


def completed_cutoff(now: datetime | None = None) -> date:
    """Last trading date whose daily bar is final. Today's bar counts only after 6pm ET."""
    now_et = (now or datetime.now(UTC)).astimezone(ET)
    return now_et.date() if now_et.time() >= time(18, 0) else now_et.date() - timedelta(days=1)


class AlpacaPrices:
    def __init__(self, key: str, secret: str):
        self.client = httpx.Client(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=30)

    def snapshots(self, tickers: list[str]) -> dict[str, dict]:
        if not tickers:
            return {}
        resp = self.client.get(f"{DATA_URL}/snapshots",
                               params={"symbols": ",".join(tickers), "feed": "iex"})
        resp.raise_for_status()
        return parse_snapshots(resp.json())

    def daily_bars(self, tickers: list[str], start: date, end: date) -> list[tuple]:
        rows: list[tuple] = []
        for i in range(0, len(tickers), 100):  # keep URLs reasonable
            params = {
                "symbols": ",".join(tickers[i:i + 100]),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": "all",
                "feed": "sip",
                "limit": 10000,
            }
            while True:
                resp = self.client.get(f"{DATA_URL}/bars", params=params)
                resp.raise_for_status()
                data = resp.json()
                rows += parse_bars(data)
                token = data.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        return rows
