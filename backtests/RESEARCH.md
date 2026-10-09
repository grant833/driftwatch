# What the evidence says (October 2026)

A research review done after the insider backtest came back negative. Four questions:
does LLM news trading work, does insider-following still work, which systematic
strategies hold up, and how should a small Roth IRA actually be run. Sources at the end
of each section; anything not verified against a primary source is marked *(unverified)*.

## 1. AI news trading: real, small, fast, fading

- **The best-known study** (Lopez-Lira & Tang, *Can ChatGPT Forecast Stock Price
  Movements?*, latest version Oct 2025) found LLM headline scores predict **next-day**
  returns: about 34 bps/day long-short before costs, after the model's training cutoff.
  - The **long side is weak** (~8 bps/day, Sharpe 0.78); most of the money is on the short
    side and in **small caps**.
  - Profitable at 5–10 bps round-trip costs, **unprofitable at 20 bps**.
  - **Fading fast:** Sharpe 6.5 (late 2021) → 3.7 (2022) → 2.3 (2023) → 1.2 (early 2024).
  - Entering 15+ minutes after a headline still captured ~30 bps; being first doesn't
    matter for this, but **no study tests a 5-day hold**.
- **Speed race is lost:** earnings news in liquid stocks is priced within seconds
  (Christensen, Timmermann & Veliyev 2024/26).
- **Hard vs soft news:** markets underreact to quantified news (earnings, guidance,
  analyst actions), which drifts for weeks, and overreact to soft news (product launches,
  commentary), which reverses (Kargarzadeh et al. 2026, preprint).
- **Follow the reaction:** after firm news, prices keep drifting in the direction of the
  first reaction for days (Jiang, Li & Wang, JFE 2021). driftwatch currently *skips*
  stocks that already moved >5%; that may be backwards. Pre-registered as Q3 below.
- **Model disagreement predicts lower returns** (Cheng, Hu & Li 2026), supporting the
  AGREE requirement, but three personas of one model are correlated, not independent.
- **LLM probabilities are overconfident** (KalshiBench 2025): a stated 0.55 isn't a
  calibrated 55%. Pre-registered as Q4.
- **Backtests before a model's training cutoff are invalid**: models memorize market
  history (Lopez-Lira, Tang & Zhu 2025). This is why the AI panel can only be tested live.

**Realistic expectation for driftwatch's design** (long-only, 5-day hold): somewhere
between 0 and +3%/yr over SPY at best, and plausibly nothing after costs.

Sources: arxiv.org/abs/2304.07619 · ideas.repec.org/a/eee/jfinec/v141y2021i2p573-599.html ·
arxiv.org/abs/2601.08962 · arxiv.org/abs/2608.14014 · arxiv.org/abs/2504.14765 ·
arxiv.org/abs/2512.23847 · arxiv.org/abs/2512.16030

## 2. Insider buying: the edge moved earlier, and into tiny stocks

- Classic studies (Lakonishok & Lee 2001; Jeng, Metrick & Zeckhauser 2003) found insider
  purchases beat the market by ~6%/yr, **mostly in small firms**, with about half the gain
  in the first month after the *trade*.
- After the 2002 rule requiring filing within two business days, purchase alphas roughly
  **halved**, and more of the move happens **at the filing itself**.
- A 2022–2026 filing-date study (QuantInsti, 7,405 officer buys) found +0.53% at 1 day and
  +1.0% at 5 days vs SPY (significant), but **nothing significant by 21 days**. A microcap
  study (Zhao 2026, preprint) found the reaction lasts 2–3 sessions and **nothing is left
  if you skip day 1**.
- **Our result fits the literature**: we enter the session after the filing date, hold 20
  sessions, and exclude stocks under $5 — exactly where the remaining edge isn't.
- One evidence-based variant remains untested on recent data: **"opportunistic" insiders**
  (Cohen, Malloy & Pomorski 2012) — people who don't trade on a routine calendar
  schedule. Their buys earned ~0.7–1.6%/month in 1986–2007. Testing it needs insiders'
  sales history too (we only loaded purchases) and a held-out period.
- **SPY may be the wrong yardstick**: small caps trailed the S&P 500 badly in 2020,
  2023 and 2024, our worst years. `backtest-diagnose` now grades against IWM too.

Sources: nber.org/papers/w16454.pdf · scholar.harvard.edu/sites/scholar.harvard.edu/files/rzeckhauser/files/insider_trading.pdf ·
blog.quantinsti.com/sec-form-4-insider-trading-python-event-study/ · arxiv.org/abs/2602.06198

## 3. Systematic strategies: most edges are small after costs

- Published anomalies lose roughly **half to two-thirds** of their returns after
  publication (McLean & Pontiff 2016); the average one nets **~8 bps/month** after
  realistic costs (Chen & Velikov 2023).
- **Post-earnings drift is gone outside microcaps** since ~2006 (Martineau 2022).
- **Trend filter on SPY** (hold SPY above its 10-month/200-day average, else T-bills):
  about the same long-run return as buy-and-hold with **much smaller crashes**
  (1901–2012: 10.2% vs 9.3%/yr, max drawdown −50% vs −83%). It lags in choppy years
  (2018, 2020, 2022). It reduces risk; it doesn't reliably add return.
- **Volatility-managed portfolios fail in real time** (Cederburg et al. 2020); **leveraged
  ETFs** can lose most of their value in a bad year.
- **79% of active large-cap funds trailed the S&P 500 in 2025** (SPIVA); the average
  investor earns ~1.2 points/yr less than their funds because of timing mistakes
  (Morningstar *Mind the Gap*).

Sources: onlinelibrary.wiley.com/doi/10.1111/jofi.12365 ·
federalreserve.gov/econres/feds/zeroing-in-on-the-expected-returns-of-anomalies.htm ·
cfr.ivo-welch.info/published/papers/martineau2021rest.pdf ·
spglobal.com/spdji/en/spiva/article/spiva-us-year-end-2025/ ·
morningstar.com/business/insights/research/mind-the-gap

## 4. Running a small Roth IRA

- 2026 limit **$7,500** (income phase-out $153k–$168k single). Contributing the maximum
  early and every year is the biggest lever: $7,500/yr for 30 years at 10% grows to about
  $1.23M; one extra point of return a year (very hard to get) adds about 20%, and
  skipping just the first year's contribution costs about 11%.
- Alpaca IRAs (since May 2026): **limited 1x margin, no shorting** (so the short side of
  the news research is off the table in the IRA). Ask support about: settlement /
  good-faith rules, fractional shares, and whether the **High-Yield Cash** sweep
  (~3.56% APY in Sept 2026) applies to IRAs — strategies sit partly in cash, so this
  matters.
- The pattern-day-trader rule was retired in June 2026 (FINRA Notice 26-10).
- **Stop-losses** only help when prices trend; under noise they lower returns (Kaminski &
  Lo 2014). In our backtest, 28% of trades exited at the stop.
- **Wash sales across accounts**: buying in the IRA within 30 days of a loss sale in a
  taxable account permanently loses that deduction (Rev. Rul. 2008-5).

Sources: irs.gov/newsroom/401k-limit-increases-to-24500-for-2026-ira-limit-increases-to-7500 ·
alpaca.markets/blog/alpaca-introduces-individual-retirement-accounts-for-trading-api-users/ ·
finra.org/rules-guidance/notices/26-10 · dspace.mit.edu/handle/1721.1/114876

## What changes in driftwatch

1. **Nothing in the live tournament changes before mid-January.** Changing rules
   mid-test would make the results meaningless.
2. **Pre-registered questions** (`experiments` command, `/experiments` in Telegram),
   answered from live calls with no extra trading, judged in mid-November:
   - Q1: is the edge in the first 1–2 sessions? (scorer now grades 1, 2, 3, 5, 10)
   - Q2: hard news vs soft news
   - Q3: calls that agree with the early price reaction vs calls that fight it
   - Q4: calibration of the panel's probabilities
   - Q5: a *fast* insider entry: each qualifying Form 4 seen during market hours is priced
     the moment the bot sees it and graded vs IWM at the close, next open, 1 and 5
     sessions (measured, never traded). `backtest-diagnose` showed the remaining insider
     edge sits in the hours after filing (+0.8% filing close to next open, 73% of the
     time) and is gone by day 2–5.
3. **Insider diagnostic** (`backtest-diagnose`): where the return goes (before filing,
   overnight, after entry) vs SPY and IWM. A diagnostic, not a new strategy.
4. **The likely end state, stated now so it can't drift later:** unless the AI panel
   clearly beats SPY on a risk-adjusted basis by January, the Roth IRA holds a broad
   index fund (optionally with the SPY trend filter), and driftwatch keeps running on
   paper as research. Any strategy that earns real money starts as a small satellite
   (≤20–30% of the account).
