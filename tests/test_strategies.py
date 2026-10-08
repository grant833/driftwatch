from datetime import date

import pytest

from driftwatch.broker import Position
from driftwatch.strategies import (
    add_trading_days,
    daily_vol,
    drawdown,
    limit_price,
    plan_exits,
    position_dollars,
    shares_for,
    trend_on,
)

ACC = {"max_positions": 10, "max_position_pct": 0.10}
TC = {"default_daily_vol": 0.03, "reference_daily_vol": 0.02}


def test_add_trading_days_skips_weekends():
    assert add_trading_days(date(2026, 10, 9), 1) == date(2026, 10, 12)   # Fri -> Mon
    assert add_trading_days(date(2026, 10, 6), 5) == date(2026, 10, 13)


def test_position_sizing_scales_with_volatility():
    assert position_dollars(100_000, 0.02, ACC, TC) == 10_000      # reference vol: full slot
    assert position_dollars(100_000, 0.04, ACC, TC) == 5_000       # twice as jumpy: half
    assert position_dollars(100_000, 0.005, ACC, TC) == 10_000     # calm stocks capped
    assert position_dollars(100_000, None, ACC, TC) == pytest.approx(100_000 * 0.1 * (2 / 3))
    assert shares_for(10_000, 61.5) == 162 and shares_for(10, 0) == 0


def test_daily_vol_needs_history():
    assert daily_vol([100, 101]) is None
    assert daily_vol([100 * 1.01 ** i for i in range(15)]) < 1e-9   # perfectly steady


def test_limit_prices_are_marketable():
    assert limit_price(100.0, "buy", 0.005) == 100.5
    assert limit_price(100.0, "sell", 0.005) == 99.5


def test_trend_and_drawdown():
    assert trend_on([1.0] * 10, 200) is None
    assert trend_on(list(range(1, 201)), 200) is True
    assert trend_on(list(range(200, 0, -1)), 200) is False
    assert drawdown(85_000, 100_000) == pytest.approx(-0.15)


def _pos(t, plpc):
    return Position(t, 10, 100, 100 * (1 + plpc), 1000, plpc)


def test_plan_exits():
    today = date(2026, 10, 15)
    held = {"STOP": {"entry_day": date(2026, 10, 13), "exit_after": date(2026, 10, 20)},
            "TIME": {"entry_day": date(2026, 10, 8), "exit_after": date(2026, 10, 15)},
            "BEAR": {"entry_day": date(2026, 10, 14), "exit_after": date(2026, 10, 21)},
            "KEEP": {"entry_day": date(2026, 10, 14), "exit_after": date(2026, 10, 21)}}
    pos = [_pos("STOP", -0.09), _pos("TIME", 0.02), _pos("BEAR", 0.01), _pos("KEEP", 0.03),
           _pos("MANUAL", -0.5)]
    exits = {e.ticker: e.reason for e in plan_exits(pos, held, today, 0.08, {"BEAR"})}
    assert set(exits) == {"STOP", "TIME", "BEAR"}       # MANUAL isn't ours: never touched
    assert exits["STOP"].startswith("stop-loss")
