-- Idempotent schema. Every table records received_at (when WE learned it),
-- separate from the source's own timestamp, so later analysis is point-in-time honest.

CREATE TABLE IF NOT EXISTS news_items (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT        NOT NULL,
    external_id   TEXT        NOT NULL,
    headline      TEXT        NOT NULL,
    summary       TEXT,
    content       TEXT,
    symbols       TEXT[]      NOT NULL DEFAULT '{}',
    url           TEXT,
    author        TEXT,
    published_at  TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    via           TEXT        NOT NULL DEFAULT 'stream',   -- 'stream' or 'backfill'
    raw           JSONB       NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS news_items_published_idx ON news_items (published_at DESC);
CREATE INDEX IF NOT EXISTS news_items_symbols_idx   ON news_items USING GIN (symbols);

CREATE TABLE IF NOT EXISTS filings (
    accession    TEXT        PRIMARY KEY,
    form_type    TEXT        NOT NULL,
    cik          TEXT        NOT NULL,
    ticker       TEXT,
    company      TEXT,
    role         TEXT,
    items        TEXT[]      NOT NULL DEFAULT '{}',
    filed_at     TIMESTAMPTZ,
    url          TEXT        NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw          JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS filings_ticker_idx ON filings (ticker, filed_at DESC);
CREATE INDEX IF NOT EXISTS filings_form_idx   ON filings (form_type, filed_at DESC);

-- First observed value per bucket is kept, so we store what we knew at the time
-- rather than GDELT's later revisions.
CREATE TABLE IF NOT EXISTS gdelt_tone (
    query_name   TEXT             NOT NULL,
    bucket_at    TIMESTAMPTZ      NOT NULL,
    tone         DOUBLE PRECISION NOT NULL,
    received_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (query_name, bucket_at)
);

-- Hash-chained, append-only prediction ledger.
CREATE TABLE IF NOT EXISTS ledger (
    seq          BIGINT      PRIMARY KEY,
    recorded_at  TIMESTAMPTZ NOT NULL,
    kind         TEXT        NOT NULL,
    body         TEXT        NOT NULL,   -- canonical JSON that was hashed
    payload      JSONB       NOT NULL,   -- same content, queryable
    prev_hash    TEXT        NOT NULL,
    hash         TEXT        NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_no_mutation ON ledger;
CREATE TRIGGER ledger_no_mutation
    BEFORE UPDATE OR DELETE ON ledger
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

DROP TRIGGER IF EXISTS ledger_no_truncate ON ledger;
CREATE TRIGGER ledger_no_truncate
    BEFORE TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();

-- ===================== Phase 1 =====================

ALTER TABLE filings ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ;
ALTER TABLE filings ADD COLUMN IF NOT EXISTS parse_error TEXT;

-- One row per non-derivative transaction line in a Form 4.
CREATE TABLE IF NOT EXISTS insider_trades (
    accession         TEXT        NOT NULL REFERENCES filings(accession),
    line_no           INT         NOT NULL,
    ticker            TEXT,
    issuer_cik        TEXT,
    insider_name      TEXT,
    officer_title     TEXT,
    is_director       BOOLEAN     NOT NULL DEFAULT false,
    is_officer        BOOLEAN     NOT NULL DEFAULT false,
    is_ten_pct_owner  BOOLEAN     NOT NULL DEFAULT false,
    transaction_date  DATE,
    code              TEXT,       -- P = open-market purchase, S = sale, A = award, ...
    acquired_disposed TEXT,       -- A or D
    shares            DOUBLE PRECISION,
    price             DOUBLE PRECISION,
    value_usd         DOUBLE PRECISION,
    shares_after      DOUBLE PRECISION,
    plan_10b5_1       BOOLEAN     NOT NULL DEFAULT false,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession, line_no)
);
CREATE INDEX IF NOT EXISTS insider_trades_ticker_idx ON insider_trades (ticker, transaction_date DESC);

-- Discretionary open-market buys by officers/directors, not pre-planned, meaningful size.
CREATE OR REPLACE VIEW insider_buy_signals AS
SELECT t.ticker, t.insider_name, t.officer_title, t.is_director, t.is_officer,
       t.transaction_date, t.shares, t.price, t.value_usd, t.shares_after,
       f.filed_at, t.received_at, t.accession
FROM insider_trades t
JOIN filings f USING (accession)
WHERE t.code = 'P'
  AND t.acquired_disposed = 'A'
  AND NOT t.plan_10b5_1
  AND (t.is_officer OR t.is_director)
  AND t.value_usd >= 25000
  AND t.ticker IS NOT NULL;

-- Stage 1: cheap triage of every headline.
CREATE TABLE IF NOT EXISTS triage (
    news_id          BIGINT      PRIMARY KEY REFERENCES news_items(id),
    material         BOOLEAN     NOT NULL,
    category         TEXT,
    market_sentiment DOUBLE PRECISION,
    model            TEXT        NOT NULL,
    prompt_version   TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stage 2: each analyst persona's independent view.
CREATE TABLE IF NOT EXISTS panel_assessments (
    id             BIGSERIAL   PRIMARY KEY,
    news_id        BIGINT      NOT NULL REFERENCES news_items(id),
    ticker         TEXT        NOT NULL,
    persona        TEXT        NOT NULL,
    model          TEXT        NOT NULL,
    prompt_version TEXT        NOT NULL,
    output         JSONB       NOT NULL,
    input_tokens   INT,
    output_tokens  INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (news_id, ticker, persona)
);

-- Aggregated panel verdict, linked to its ledger entry.
CREATE TABLE IF NOT EXISTS predictions (
    news_id        BIGINT      NOT NULL REFERENCES news_items(id),
    ticker         TEXT        NOT NULL,
    ledger_seq     BIGINT      NOT NULL REFERENCES ledger(seq),
    p_up_mean      DOUBLE PRECISION NOT NULL,
    p_up_std       DOUBLE PRECISION NOT NULL,
    novelty_mean   DOUBLE PRECISION NOT NULL,
    agree          BOOLEAN     NOT NULL,
    magnitude      TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (news_id, ticker)
);

-- Replacement for GDELT: market mood from our own headlines.
CREATE OR REPLACE VIEW market_mood_hourly AS
SELECT date_trunc('hour', n.published_at) AS hour,
       avg(t.market_sentiment)            AS mood,
       count(*)                           AS headlines
FROM triage t JOIN news_items n ON n.id = t.news_id
WHERE t.market_sentiment IS NOT NULL
GROUP BY 1;

-- Every API call, for daily caps and cost tracking.
CREATE TABLE IF NOT EXISTS llm_calls (
    id             BIGSERIAL   PRIMARY KEY,
    stage          TEXT        NOT NULL,   -- triage | panel
    model          TEXT        NOT NULL,
    input_tokens   INT         NOT NULL,
    output_tokens  INT         NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_day_idx ON llm_calls (created_at);

-- ===================== Phase 1.1 =====================

-- Fund/ETF tickers from SEC's investment-company list; never traded.
CREATE TABLE IF NOT EXISTS fund_tickers (
    ticker       TEXT        PRIMARY KEY,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which symbols the article is actually about (vs. merely mentioned).
ALTER TABLE triage      ADD COLUMN IF NOT EXISTS relevant_tickers TEXT[];
-- agree | split | neutral
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS stance TEXT;

-- ===================== Phase 1.2: notifier, scorekeeper =====================

-- Outbox: any service can queue a message; the notifier delivers it.
CREATE TABLE IF NOT EXISTS notifications (
    id          BIGSERIAL   PRIMARY KEY,
    kind        TEXT        NOT NULL,     -- alert | insider | signal | summary | trade
    text        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS notifications_unsent_idx ON notifications (id) WHERE sent_at IS NULL;

-- Runtime switches (e.g. trading_halted) and small bits of service state.
CREATE TABLE IF NOT EXISTS controls (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Split- and dividend-adjusted daily bars.
CREATE TABLE IF NOT EXISTS prices_daily (
    ticker  TEXT             NOT NULL,
    day     DATE             NOT NULL,
    open    DOUBLE PRECISION NOT NULL,
    close   DOUBLE PRECISION NOT NULL,
    volume  DOUBLE PRECISION,
    PRIMARY KEY (ticker, day)
);

-- Price context captured at the moment of each prediction.
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS pre_move  DOUBLE PRECISION; -- vs prior close
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS ref_price DOUBLE PRECISION;

-- Realized outcome of each prediction at each horizon.
CREATE TABLE IF NOT EXISTS outcomes (
    news_id     BIGINT           NOT NULL,
    ticker      TEXT             NOT NULL,
    horizon     INT              NOT NULL,          -- trading sessions
    entry_day   DATE             NOT NULL,
    entry_kind  TEXT             NOT NULL,          -- open | close
    entry_px    DOUBLE PRECISION NOT NULL,
    exit_day    DATE             NOT NULL,
    exit_px     DOUBLE PRECISION NOT NULL,
    ret         DOUBLE PRECISION NOT NULL,
    spy_ret     DOUBLE PRECISION NOT NULL,
    excess      DOUBLE PRECISION NOT NULL,
    scored_at   TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (news_id, ticker, horizon),
    FOREIGN KEY (news_id, ticker) REFERENCES predictions (news_id, ticker)
);

-- ===================== Phase 1.3: universe, triage priority =====================

-- US exchange-listed stocks Alpaca can trade (refreshed daily; OTC excluded).
CREATE TABLE IF NOT EXISTS tradable_assets (
    ticker       TEXT        PRIMARY KEY,
    exchange     TEXT,
    name         TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 1 (trivial) .. 5 (major, likely to move the stock for days)
ALTER TABLE triage ADD COLUMN IF NOT EXISTS importance INT;
