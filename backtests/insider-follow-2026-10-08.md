# Insider-buy strategy: historical backtest (2026-10-08)

Rules follow the live `insider` paper account (slots, sizing, chase limit, stop-loss, 20-session hold, half the slots when SPY is below its 200-day average, the 3% daily-loss brake and the 15% drawdown halt). Buy at the open of the first session after the Form 4 filing date; graded against SPY over the same window. Differences from live trading are listed under Limitations.
Config registered in the ledger before results were computed (entry #491); results recorded as entry #492.

## Data

- Qualifying signals: 39,565 (officer/director open-market buys ≥ $25,000, price ≥ $5, since 2016-01-01)
- Priced by Alpaca: 38,387 (97% coverage)
- Skipped: {"fund ticker": 310, "fund or SPAC (SEC industry code)": 167, "already ran >5% (don't chase)": 4800, "no price data": 1178}
- SEC quarters missing: none
- Bars stopped before the exit (delisted or halted): 34

## Pre-registered test: does the stock beat SPY after an insider buy?

| Horizon | Events | Hit rate | Median vs SPY | Capped mean | Beta-adjusted mean | Months | Month t-stat | Months positive |
|---|---|---|---|---|---|---|---|---|
| 5 sessions | 33,587 | 49% | -0.06% | +0.09% | +0.13% | 129 | 2.4 | 62% |
| 10 sessions | 33,552 | 49% | -0.08% | +0.06% | -0.01% | 129 | 1.3 | 58% |
| 20 sessions | 33,457 | 48% | -0.33% | -0.15% | +0.04% | 129 | -0.1 | 55% |

The month t-stat averages each month's events first, so a crowded month counts once. Above ~2 is meaningful; above ~3 is strong.

## Exploratory slices (20 sessions) — not pre-registered

| Slice | Events | Hit rate | Median | Capped mean | Month t |
|---|---|---|---|---|---|
| all signals (pre-registered) | 33,457 | 48% | -0.33% | -0.15% | -0.1 |
| cluster: 2+ insiders | 4,938 | 49% | -0.17% | +0.02% | 0.8 |
| big: $250k+ | 11,578 | 49% | -0.26% | -0.05% | 0.4 |
| CEO/CFO/President/Chair | 11,088 | 48% | -0.27% | -0.11% | -0.1 |
| directors only | 19,038 | 48% | -0.40% | -0.23% | -0.5 |

Five slices were examined; expect one to look good by luck alone.

## By year (20 sessions, all signals)

| Year | Events | Hit rate | Median | Capped mean |
|---|---|---|---|---|
| 2016 | 3,384 | 58% | +1.19% | +1.99% |
| 2017 | 3,046 | 48% | -0.37% | +0.21% |
| 2018 | 3,877 | 53% | +0.53% | +0.71% |
| 2019 | 3,244 | 45% | -0.59% | -0.46% |
| 2020 | 3,964 | 42% | -2.37% | -2.31% |
| 2021 | 3,078 | 45% | -0.92% | -0.57% |
| 2022 | 3,399 | 51% | +0.14% | +0.00% |
| 2023 | 2,919 | 44% | -1.28% | -0.93% |
| 2024 | 2,073 | 40% | -1.52% | -0.98% |
| 2025 | 2,625 | 49% | -0.09% | +0.49% |
| 2026 | 1,848 | 49% | -0.11% | +0.31% |

## Portfolio simulation (2016-01-05 to 2026-10-07, 10 bps costs per side)

| | Strategy | SPY buy & hold | SPY at same exposure |
|---|---|---|---|
| Annual return | +5.3% | +15.1% | +9.5% |
| Volatility | 13.3% | 17.5% | |
| Sharpe | 0.45 | 0.90 | |
| Max drawdown | -30.6% | -33.8% | |

- Average invested: 67% (the rest sits in cash, earning nothing in this simulation)
- Trades: 1,787; win rate 49%; average +0.47%, median -0.20%
- Exits: {"data ended": 3, "stop": 377, "stop (gap)": 124, "time": 1283}
- Drawdown halts: 5; sessions with new entries blocked by a safety rule: 26
- P(daily returns beat SPY): 2%; P(beat SPY at the same exposure): 14%

| Year | Strategy | SPY |
|---|---|---|
| 2016 | +11.3% | +12.8% |
| 2017 | +22.1% | +21.7% |
| 2018 | -0.2% | -5.0% |
| 2019 | +20.7% | +31.1% |
| 2020 | +11.4% | +18.5% |
| 2021 | +3.0% | +28.6% |
| 2022 | -15.3% | -18.2% |
| 2023 | +4.5% | +26.2% |
| 2024 | -1.3% | +24.9% |
| 2025 | +6.8% | +17.7% |
| 2026 | -0.6% | +14.9% |

## Limitations (read these before trusting the numbers)

- Survivorship: stocks without Alpaca price history drop out; delisted names skew toward bad outcomes, so results may be optimistic.
- Industry codes and the fund list are today's: a company that was a SPAC when insiders bought may now be an operating company and slip through.
- Rule 10b5-1 plan flags only exist in filings since 2023 (excluded when present).
- Entry is the next session's open after the filing *date*; live trading often acts the same day for filings made during market hours, and decides at 9:45 rather than the open.
- Stop-losses are checked at the open and close only (live: three times a day).
- The $5 minimum uses the insider's reported price, not the market price; the 'don't chase' check compares to the adjusted close on the insider's trade date.
- The 15% drawdown halt is lifted after 5 sessions (live: when you send /resume confirm).
- Cash earns 0% here; in reality idle cash earns interest.
- The live account also favours stocks with bullish AI news; that can't be replayed historically.
- Statistics aren't deflated for the 5 slices and 3 live strategies; treat anything short of a strong pre-registered result as unproven.
