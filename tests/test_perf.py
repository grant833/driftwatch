import pytest

from driftwatch.perf import account_stats, expected_max_sharpe, max_drawdown, psr


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(-0.25)


def test_psr_grows_with_evidence():
    good = [0.002, -0.001, 0.003, 0.001, -0.002, 0.002] * 5
    assert psr(good[:6]) < psr(good) < 1
    assert psr([0.01, -0.01, 0.01, -0.01] * 10) == pytest.approx(0.5, abs=0.05)


def test_expected_max_sharpe_rises_with_trials():
    assert expected_max_sharpe(0.01, 1) == 0
    assert 0 < expected_max_sharpe(0.01, 3) < expected_max_sharpe(0.01, 30)


def test_account_stats_vs_spy():
    s = account_stats([100, 102, 104], [400, 400, 404])
    assert s["ret"] == pytest.approx(0.04) and s["spy_ret"] == pytest.approx(0.01)
    assert s["excess"] == pytest.approx(0.03)
