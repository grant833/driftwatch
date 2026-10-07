"""Alpaca market data: live snapshots (for the "already priced in?" check) and
split/dividend-adjusted daily bars (for scoring predictions).

Free-plan notes: snapshots use the IEX feed (real time, but only ~2-3% of volume,
so thin stocks can show stale prices). Historical SIP bars are free once they are
more than 15 minutes old, which is all the scorekeeper needs.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2/stocks"
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)
VALID_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


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


def valid_symbol(t: str) -> bool:
    """US-listed equity symbols: 1-5 letters, optional share class (BRK.B).
    Rejects crypto pairs (BTCUSD), foreign listings (TSX:GDL) and other junk."""
    return bool(VALID_SYMBOL.match(t or ""))


def bars_end(end: date, now: datetime | None = None) -> str:
    """End timestamp for a bars request. Free plans may not touch the last 15 minutes
    of SIP data (Alpaca answers 403), so never ask past now-20min."""
    now = now or datetime.now(UTC)
    day_end = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=ET)
    return min(day_end.astimezone(UTC), now - timedelta(minutes=20)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_assets(data, exclude_otc: bool = True) -> dict[str, tuple[str, str]]:
    """Alpaca /v2/assets -> {ticker: (exchange, name)} for active, tradable US stocks."""
    out = {}
    for a in data if isinstance(data, list) else []:
        sym = str(a.get("symbol") or "").upper()
        if not (a.get("tradable") and a.get("status", "active") == "active" and valid_symbol(sym)):
            continue
        if a.get("class") not in (None, "us_equity"):
            continue
        exchange = str(a.get("exchange") or "")
        if exclude_otc and exchange.upper() == "OTC":
            continue
        out[sym] = (exchange, str(a.get("name") or ""))
    return out


class AlpacaPrices:
    def __init__(self, key: str, secret: str, trading_url: str | None = None):
        self.client = httpx.Client(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=60)
        self.trading_url = (trading_url or os.getenv("ALPACA_TRADING_URL")
                            or "https://paper-api.alpaca.markets").rstrip("/")
        self.bad: set[str] = set()   # symbols Alpaca rejected; never ask again this run

    def snapshots(self, tickers: list[str]) -> dict[str, dict]:
        tickers = [t for t in tickers if valid_symbol(t)]
        if not tickers:
            return {}
        resp = self.client.get(f"{DATA_URL}/snapshots",
                               params={"symbols": ",".join(tickers), "feed": "iex"})
        resp.raise_for_status()
        return parse_snapshots(resp.json())

    def tradable_assets(self, exclude_otc: bool = True) -> dict[str, tuple[str, str]]:
        resp = self.client.get(f"{self.trading_url}/v2/assets",
                               params={"status": "active", "asset_class": "us_equity"})
        resp.raise_for_status()
        return parse_assets(resp.json(), exclude_otc)

    def daily_bars(self, tickers: list[str], start: date, end: date,
                   now: datetime | None = None) -> list[tuple]:
        tickers = sorted({t for t in tickers if valid_symbol(t)} - self.bad)
        end_ts = bars_end(end, now)
        rows: list[tuple] = []
        for i in range(0, len(tickers), 100):  # keep URLs reasonable
            rows += self._bars(tickers[i:i + 100], start.isoformat(), end_ts)
        return rows

    def _bars(self, symbols: list[str], start: str, end: str) -> list[tuple]:
        """Fetch one batch. If Alpaca rejects the batch as a bad request, split it in
        half until the offending symbol is isolated, then skip just that symbol."""
        if not symbols:
            return []
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                  "end": end, "adjustment": "all", "feed": "sip", "limit": 10000}
        rows: list[tuple] = []
        while True:
            resp = self.client.get(f"{DATA_URL}/bars", params=params)
            if resp.status_code == 400:
                if len(symbols) == 1:
                    log.warning("Alpaca rejected symbol %s; skipping it", symbols[0])
                    self.bad.add(symbols[0])
                    return []
                mid = len(symbols) // 2
                return self._bars(symbols[:mid], start, end) + self._bars(symbols[mid:],
                                                                          start, end)
            if resp.status_code == 403:
                raise PermissionError(f"Alpaca refused bar data (403): {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            rows += parse_bars(data)
            token = data.get("next_page_token")
            if not token:
                return rows
            params["page_token"] = token
