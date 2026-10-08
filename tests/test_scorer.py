from datetime import date, datetime

from driftwatch.prices import ET
from driftwatch.scorer import entry_point, exit_index, score_one, summarize

# Mon Oct 5 .. Fri Oct 9, then Mon Oct 12, 2026
DAYS = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12)]


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=ET)


def test_entry_rules():
    assert entry_point(at(6, 8, 15), DAYS) == (1, "open")     # premarket -> same-day open
    assert entry_point(at(6, 11), DAYS) == (1, "close")       # intraday -> same-day close
    assert entry_point(at(6, 16, 30), DAYS) == (2, "open")    # after close -> next open
    assert entry_point(at(10, 12), DAYS) == (5, "open")       # Saturday -> Monday open
    assert entry_point(at(12, 17), DAYS) is None              # next session not in data yet


def test_exit_index():
    assert exit_index(1, "open", 1) == 1    # open entry: 1-day exits at that day's close
    assert exit_index(1, "close", 1) == 2   # close entry: 1-day exits next close
    assert exit_index(1, "close", 5) == 6


def test_score_one_is_excess_vs_spy():
    px = {}
    for d in DAYS:
        px[("SPY", d)] = (100.0, 100.0)
        px[("ABC", d)] = (50.0, 50.0)
    px[("ABC", date(2026, 10, 7))] = (50.0, 55.0)   # ABC +10% by Wed close
    px[("SPY", date(2026, 10, 7))] = (100.0, 102.0)  # SPY +2%
    row = score_one(at(6, 11), "ABC", 1, DAYS, px)   # enter Tue close, exit Wed close
    assert row["entry_kind"] == "close" and row["exit_day"] == date(2026, 10, 7)
    assert abs(row["excess"] - 0.08) < 1e-9
    assert score_one(at(6, 11), "ABC", 10, DAYS, px) is None   # horizon not reached
    assert score_one(at(6, 11), "ZZZ", 1, DAYS, px) is None    # no price data


def test_summarize():
    rows = [(0.6, 0.02, "agree"), (0.6, -0.01, "split"), (0.4, -0.03, "agree"),
            (0.5, 0.05, "neutral")]
    s = summarize(rows)
    assert s["n"] == 4 and s["n_dir"] == 3
    assert abs(s["hit_rate"] - 2 / 3) < 1e-9
    assert s["agree"]["n"] == 2 and s["agree"]["hit_rate"] == 1.0
    assert s["split"]["hit_rate"] == 0.0
    assert summarize([]) == {"n": 0, "n_dir": 0}


def test_summarize_resists_outliers_and_counts_days():
    from datetime import date
    d1, d2 = date(2026, 10, 6), date(2026, 10, 7)
    rows = [(0.4, -0.70, "agree", d1),            # one wild short winner (+70%)
            (0.6, -0.01, "agree", d1), (0.6, -0.02, "split", d2), (0.6, -0.01, "agree", d2)]
    s = summarize(rows)
    assert s["mean_signed"] > 0.15                 # the raw average is fooled...
    assert s["median_signed"] < 0                  # ...the median is not
    assert s["clipped_mean"] < s["mean_signed"]
    assert s["n_days"] == 2 and "rank_ic" in s


def test_pre_entry_beta_ignores_prices_after_entry():
    from datetime import date, timedelta

    from driftwatch.scorer import pre_entry_beta
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    px = {}
    for i, d in enumerate(days):
        m = 100 * (1 + 0.01 * ((i % 5) - 2))
        px[("SPY", d)] = (m, m)
        s = 50 * (1 + 0.02 * ((i % 5) - 2)) if i < 100 else 50 * (1 + 0.5 * (i % 2))
        px[("AAA", d)] = (s, s)
    b = pre_entry_beta("AAA", days[100], days, px)
    assert b is not None and 1.5 < b < 2.5          # the wild days after entry don't count
    assert pre_entry_beta("AAA", days[20], days, px) is None   # too little history
