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
