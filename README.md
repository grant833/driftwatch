# driftwatch

A slow, skeptical, provable news-driven trading research system.

Most retail bots try to be fast. driftwatch assumes it can't win a speed race and
instead looks for news the market hasn't fully absorbed, holds for days, and
records every prediction in a tamper-evident ledger, so its track record can be
proven instead of claimed.

## Status: Phase 2 (paper-trading tournament)

| Component | What it does |
|---|---|
| `news` service | Alpaca/Benzinga news over WebSocket, with automatic REST gap-fill on every reconnect |
| `edgar` service | Polls SEC EDGAR for new 8-K (material events) and Form 4 (insider trades) filings, maps CIK to ticker |
| `insider` service | Parses every Form 4, flags discretionary open-market buys by officers/directors (code P, no 10b5-1 plan, ≥ $25k) |
| `analyst` service | Stage 1: cheap model triages every headline in batches and scores market-wide mood. Stage 2: three analyst personas (fundamental, skeptic, flow) independently estimate P(stock beats SPY over 5 days); agreement becomes confidence; every verdict is written to the ledger |
| `scorer` service | Pulls split/dividend-adjusted daily bars and grades every prediction at 1, 5 and 10 sessions as excess return vs SPY, using conservative point-in-time entry rules (premarket → that day's open, intraday → that day's close, after hours → next open) |
| `notifier` service | Telegram bot: insider buys, strong AGREE calls, problems, and a weekday after-close summary; commands `/status /today /insiders /score /mood /costs /kill /resume`. Obeys only the owner's chat |
| Universe & budget | Only US exchange-listed stocks Alpaca can trade, priced at $5+, ETFs excluded. Triage rates importance 1–5; the panel scores the most important news first, and its daily call budget is released evenly through the US/Eastern day so a busy morning can't starve after-close earnings |
| "Priced in?" check | At prediction time the panel sees how far the stock has already moved vs the prior close (IEX snapshot), and the move is stored with the prediction |
| `trader` service | Paper trading with three Alpaca paper accounts competing: **news** (the AI panel: AGREE, p_up ≥ 0.55, not already priced in, 5-day holds), **insider** (officer/director open-market buys, 20-day holds) and **baseline** (SPY while above its 200-day average, otherwise cash: no AI at all). Marketable limit orders only, volatility-scaled sizes, max 10% per stock, no margin, stop-losses, time exits, exit when the panel turns bearish, half the slots when SPY is in a downtrend, a 3% daily-loss brake and a 15% drawdown halt. Every order is written to the hash-chained ledger first |
| Safety | Refuses any non-paper Alpaca URL unless `trading.allow_live: true`. Telegram `/kill` halts and cancels working orders; `/flatten confirm` sells everything and halts; `/resume confirm` restarts |
| `/perf` | Each account vs buy-and-hold SPY: return, max drawdown, Sharpe, probabilistic Sharpe, and the Deflated Sharpe Ratio adjusted for the number of strategies tried |
| `gdelt` service | Optional, off by default (GDELT's free API returned empty data in Oct 2026); replaced by headline-based market mood |
| Ledger | Hash-chained, append-only (enforced by Postgres triggers), daily anchors for public git timestamps |
| Guards | Ticker blocklist so the bot never buys ETFs held in the UBS model (wash-sale protection) |

No trading happens yet. Hard daily caps on API calls are enforced in code, and every call is logged for cost tracking.

## Design principles

- **Point-in-time honesty.** Every row stores `received_at` (when we learned it) separately from the
  source's timestamp. Backtests can only use what was actually known at the time.
- **Idempotent ingestion.** Every insert is `ON CONFLICT DO NOTHING`; services can restart freely.
- **No silent gaps.** The news stream backfills from its last stored article whenever it reconnects.
- **Append-only evidence.** Ledger rows cannot be updated, deleted, or truncated, and any edit to
  history breaks the hash chain.

## Quickstart

```bash
cp .env.example .env          # add Alpaca PAPER keys, SEC user agent with your email
docker compose up -d --build  # starts Postgres, runs migrations, starts all three ingestors
docker compose logs -f news   # watch headlines arrive

docker compose run --rm news health              # row counts for the last 24h
docker compose run --rm news ledger-note "hello" # append a ledger entry
docker compose run --rm news ledger-verify       # check the hash chain
docker compose run --rm news ledger-anchor       # write anchors/YYYY-MM-DD.txt, then git commit + push
docker compose run --rm news check-ticker VOO    # test the blocklist
docker compose run --rm news predictions         # latest panel verdicts
docker compose run --rm news insiders            # latest insider buy signals
docker compose run --rm news mood                # hourly market mood from headlines
docker compose run --rm news costs               # API calls and tokens per day
docker compose run --rm news score               # scorecard: hit rate, edge, IC vs SPY
docker compose run --rm news kill                # halt trading (same as /kill in Telegram)
docker compose run --rm news positions           # open paper positions
docker compose run --rm news perf                # tournament results vs SPY
```

Runs on a Raspberry Pi 5 (arm64) or any small Linux VM.

## Setup checklist

1. Create an Alpaca account and generate **paper** API keys.
2. Replace `config/blocklist.txt` with every ETF in the UBS model.
3. Set `SEC_USER_AGENT` to your name and real email (SEC requirement).
4. Optional: add a Slack incoming webhook for alerts.
5. Schedule `ledger-anchor` daily (cron) and commit the anchor file to a public repo.

## Roadmap

- **Phase 1 (done):** Form 4 parsing, LLM triage plus analyst panel, predictions in the ledger.
- **Phase 1C (done):** "already priced in?" check, scorekeeper, Telegram bot with kill switch.
- **Phase 2 (running):** Three-way paper tournament (AI news, insider buys, SPY trend baseline).
- **Phase 3:** Go/no-go gate on Deflated Sharpe vs SPY, then small live Roth IRA allocation.

## Tests

```bash
pip install -e ".[dev]"
pytest -q                                   # unit tests
TEST_DATABASE_URL=postgresql://... pytest   # adds Postgres integration tests (CI runs these)
```
