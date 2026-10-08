from datetime import UTC, datetime

import pytest

from driftwatch.broker import check_url, parse_account, parse_clock, parse_positions
from driftwatch.trader import client_id, due_slot


def test_live_money_guard():
    assert check_url("https://paper-api.alpaca.markets/", False).endswith("alpaca.markets")
    with pytest.raises(SystemExit):
        check_url("https://api.alpaca.markets", False)       # live endpoint refused
    assert check_url("https://api.alpaca.markets", True)


def test_parsers_handle_string_numbers():
    a = parse_account({"equity": "100000.5", "last_equity": "99000", "cash": "50000",
                       "trading_blocked": False})
    assert a.equity == 100000.5 and not a.blocked
    p = parse_positions([{"symbol": "aapl", "qty": "10", "avg_entry_price": "200",
                          "current_price": "210", "market_value": "2100",
                          "unrealized_plpc": "0.05", "side": "long"},
                         {"symbol": "X", "qty": "-1", "side": "short"}])
    assert len(p) == 1 and p[0].ticker == "AAPL" and p[0].unrealized_plpc == 0.05
    c = parse_clock({"is_open": True, "timestamp": "2026-10-08T14:00:00Z",
                     "next_open": "2026-10-09T13:30:00Z", "next_close": "2026-10-08T20:00:00Z"})
    assert c.is_open and c.next_close.hour == 20


def test_client_ids_are_deterministic_and_short():
    cid = client_id("insider", "BRK.B", "buy", datetime(2026, 10, 8).date())
    assert cid == "dw-insider-BRK.B-buy-20261008" and len(cid) <= 128


def test_due_slot_collapses_missed_runs():
    times = ["09:45", "12:30", "15:45"]
    early = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)            # 9:00 ET
    assert due_slot(early, times, None) is None
    late = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)             # 2:00 ET: only latest runs
    assert due_slot(late, times, None) == "2026-10-08T12:30"
    assert due_slot(late, times, "2026-10-08T12:30") is None
