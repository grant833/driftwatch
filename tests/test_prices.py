from datetime import UTC, date, datetime

from driftwatch.prices import bar_day, completed_cutoff, parse_bars, parse_snapshots

SNAP = {"AAPL": {"latestTrade": {"t": "2026-10-06T14:00:00Z", "p": 210.0},
                 "prevDailyBar": {"c": 200.0}},
        "THIN": {"latestTrade": None, "prevDailyBar": {"c": 5.0}}}


def test_snapshots_both_layouts():
    out = parse_snapshots(SNAP)
    assert out["AAPL"]["price"] == 210.0 and out["AAPL"]["prev_close"] == 200.0
    assert "THIN" not in out
    assert parse_snapshots({"snapshots": SNAP}) == out


def test_bar_day_uses_eastern_date():
    assert bar_day("2026-10-06T04:00:00Z") == date(2026, 10, 6)
    rows = parse_bars({"bars": {"spy": [{"t": "2026-10-06T04:00:00Z", "o": 1, "c": 2, "v": 3}]}})
    assert rows == [("SPY", date(2026, 10, 6), 1.0, 2.0, 3.0)]


def test_completed_cutoff():
    # 3pm ET Oct 6 -> today's bar not final yet
    assert completed_cutoff(datetime(2026, 10, 6, 19, 0, tzinfo=UTC)) == date(2026, 10, 5)
    # 7pm ET Oct 6 -> today's bar is final
    assert completed_cutoff(datetime(2026, 10, 6, 23, 0, tzinfo=UTC)) == date(2026, 10, 6)


def test_valid_symbol():
    from driftwatch.prices import valid_symbol
    assert all(valid_symbol(t) for t in ("A", "AAPL", "CHARR", "BRK.B"))
    assert not any(valid_symbol(t) for t in ("BTCUSD", "TSX:GDL", "aapl", "", "AB1"))


def test_bars_end_never_touches_last_15_minutes():
    from driftwatch.prices import bars_end
    now = datetime(2026, 10, 6, 22, 30, tzinfo=UTC)          # 6:30pm ET
    assert bars_end(date(2026, 10, 6), now) == "2026-10-06T22:10:00Z"
    assert bars_end(date(2026, 10, 5), now) == "2026-10-06T04:00:00Z"   # end of Oct 5 ET


def test_parse_assets_filters():
    from driftwatch.prices import parse_assets
    data = [
        {"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True, "status": "active",
         "class": "us_equity", "name": "Apple"},
        {"symbol": "CURLF", "exchange": "OTC", "tradable": True, "status": "active"},
        {"symbol": "DEAD", "exchange": "NYSE", "tradable": False, "status": "active"},
        {"symbol": "BTC/USD", "exchange": "CRYPTO", "tradable": True, "class": "crypto"},
    ]
    assert parse_assets(data) == {"AAPL": ("NASDAQ", "Apple")}
    assert "CURLF" in parse_assets(data, exclude_otc=False)


def _client(handler):
    import httpx

    from driftwatch.prices import AlpacaPrices
    p = AlpacaPrices("k", "s")
    p.client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_bad_symbol_is_isolated_not_fatal():
    import httpx
    calls = []

    def handler(req):
        syms = req.url.params["symbols"].split(",")
        calls.append(syms)
        if "ZZZZ" in syms:
            return httpx.Response(400, json={"message": "invalid symbol"})
        return httpx.Response(200, json={"bars": {s: [{"t": "2026-10-06T04:00:00Z", "o": 1,
                                                       "c": 2, "v": 3}] for s in syms}})

    p = _client(handler)
    now = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    rows = p.daily_bars(["AAPL", "MSFT", "ZZZZ", "TSX:GDL", "SPY"], date(2026, 10, 1),
                        date(2026, 10, 6), now=now)
    assert sorted(r[0] for r in rows) == ["AAPL", "MSFT", "SPY"]
    assert p.bad == {"ZZZZ"} and all("TSX:GDL" not in c for c in calls)
    calls.clear()
    p.daily_bars(["ZZZZ", "AAPL"], date(2026, 10, 1), date(2026, 10, 6), now=now)
    assert calls == [["AAPL"]]                       # known-bad symbol never re-requested


def test_403_raises_clear_error():
    import httpx
    import pytest
    p = _client(lambda req: httpx.Response(403, text="subscription does not permit"))
    with pytest.raises(PermissionError):
        p.daily_bars(["AAPL"], date(2026, 10, 1), date(2026, 10, 6))
