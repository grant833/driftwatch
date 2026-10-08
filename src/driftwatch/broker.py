"""Alpaca trading API client (orders, positions, account, clock).

Safety: the client refuses any base URL that isn't Alpaca's paper endpoint unless
settings explicitly set trading.allow_live: true. Real money can never be touched
by accident or by a typo in .env.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

log = logging.getLogger(__name__)
PAPER_URL = "https://paper-api.alpaca.markets"
FINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped",
         "suspended"}


@dataclass
class Account:
    equity: float
    last_equity: float
    cash: float
    blocked: bool


@dataclass
class Position:
    ticker: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_plpc: float


@dataclass
class Clock:
    is_open: bool
    timestamp: datetime
    next_open: datetime
    next_close: datetime


class Broker(Protocol):
    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def clock(self) -> Clock: ...
    def submit(self, ticker: str, side: str, qty: int, limit_price: float,
               client_order_id: str) -> dict: ...
    def order(self, client_order_id: str) -> dict | None: ...
    def cancel_open(self) -> None: ...


class BrokerError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"{status}: {text[:300]}")
        self.status = status


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(x: str) -> datetime:
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def parse_account(d: dict) -> Account:
    return Account(_f(d.get("equity")), _f(d.get("last_equity")), _f(d.get("cash")),
                   bool(d.get("trading_blocked") or d.get("account_blocked")))


def parse_positions(rows: list) -> list[Position]:
    out = []
    for p in rows or []:
        if str(p.get("side", "long")) != "long":
            continue
        out.append(Position(str(p["symbol"]).upper(), _f(p.get("qty")),
                            _f(p.get("avg_entry_price")), _f(p.get("current_price")),
                            _f(p.get("market_value")), _f(p.get("unrealized_plpc"))))
    return out


def parse_clock(d: dict) -> Clock:
    return Clock(bool(d.get("is_open")), _ts(d["timestamp"]), _ts(d["next_open"]),
                 _ts(d["next_close"]))


def check_url(url: str, allow_live: bool) -> str:
    url = url.rstrip("/")
    if url != PAPER_URL and not allow_live:
        raise SystemExit(f"Refusing to trade against {url}: only {PAPER_URL} is allowed. "
                         "Set trading.allow_live: true only when you deliberately go live.")
    return url


class AlpacaBroker:
    def __init__(self, key: str, secret: str, base_url: str = PAPER_URL,
                 allow_live: bool = False):
        self.base = check_url(base_url, allow_live)
        self.client = httpx.Client(
            base_url=self.base, timeout=30,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _get(self, path: str, **params):
        r = self.client.get(path, params=params or None)
        if r.status_code >= 400:
            raise BrokerError(r.status_code, r.text)
        return r.json()

    def account(self) -> Account:
        return parse_account(self._get("/v2/account"))

    def positions(self) -> list[Position]:
        return parse_positions(self._get("/v2/positions"))

    def clock(self) -> Clock:
        return parse_clock(self._get("/v2/clock"))

    def submit(self, ticker, side, qty, limit_price, client_order_id) -> dict:
        body = {"symbol": ticker, "qty": str(int(qty)), "side": side, "type": "limit",
                "time_in_force": "day", "limit_price": f"{limit_price:.2f}",
                "client_order_id": client_order_id}
        r = self.client.post("/v2/orders", json=body)
        if r.status_code >= 400:
            raise BrokerError(r.status_code, r.text)
        return r.json()

    def order(self, client_order_id: str) -> dict | None:
        """Look one order up by our own id; None if Alpaca has never seen it."""
        r = self.client.get("/v2/orders:by_client_order_id",
                            params={"client_order_id": client_order_id})
        if r.status_code in (404, 422):
            return None
        if r.status_code >= 400:
            raise BrokerError(r.status_code, r.text)
        return r.json()

    def cancel_open(self) -> None:
        r = self.client.delete("/v2/orders")
        if r.status_code >= 400 and r.status_code != 404:
            raise BrokerError(r.status_code, r.text)
