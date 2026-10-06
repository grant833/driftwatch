# driftwatch

A slow, skeptical, provable news-driven trading research system.

Most retail bots try to be fast. driftwatch assumes it can't win a speed race and
instead looks for news the market hasn't fully absorbed, holds for days, and
records every prediction in a tamper-evident ledger, so its track record can be
proven instead of claimed.

## Status: Phase 0 (data foundation)

| Component | What it does |
|---|---|
| `news` service | Alpaca/Benzinga news over WebSocket, with automatic REST gap-fill on every reconnect |
| `edgar` service | Polls SEC EDGAR for new 8-K (material events) and Form 4 (insider trades) filings, maps CIK to ticker |
| `gdelt` service | Global news tone every 15 minutes, used later as a market-wide risk dial |
| Ledger | Hash-chained, append-only (enforced by Postgres triggers), daily anchors for public git timestamps |
| Guards | Ticker blocklist so the bot never buys ETFs held in the UBS model (wash-sale protection) |

No trading happens in Phase 0.

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
```

Runs on a Raspberry Pi 5 (arm64) or any small Linux VM.

## Setup checklist

1. Create an Alpaca account and generate **paper** API keys.
2. Replace `config/blocklist.txt` with every ETF in the UBS model.
3. Set `SEC_USER_AGENT` to your name and real email (SEC requirement).
4. Optional: add a Slack incoming webhook for alerts.
5. Schedule `ledger-anchor` daily (cron) and commit the anchor file to a public repo.

## Roadmap

- **Phase 1:** Form 4 XML parsing (open-market purchases, code P), LLM triage plus analyst panel,
  "already priced in?" check, predictions written to the ledger. No trading.
- **Phase 2:** Three-way paper tournament (news drift, news plus insider, SPY trend baseline).
- **Phase 3:** Go/no-go gate on Deflated Sharpe vs SPY, then small live Roth IRA allocation.

## Tests

```bash
pip install -e ".[dev]"
pytest -q                                   # unit tests
TEST_DATABASE_URL=postgresql://... pytest   # adds Postgres integration tests (CI runs these)
```
