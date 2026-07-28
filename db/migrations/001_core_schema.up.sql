-- 001_core_schema — securities, data-source provenance, and the shared updated_at trigger.
--
-- migrate: non-destructive
--
-- Everything else keys off `securities`. Provenance is here rather than bolted on later because
-- "which pull produced this row" stops being answerable once the rows exist without it — and the
-- evaluation framework's whole claim to honesty rests on being able to reconstruct what was known
-- at a point in time.
--
-- Conventions (from SENIOR_ENGINEER_BAR §4 and 9b's ADR-001):
--   * BIGINT GENERATED ALWAYS AS IDENTITY for surrogate keys
--   * TIMESTAMPTZ always, never naive timestamp
--   * TEXT with a CHECK, never VARCHAR(n)
--   * NUMERIC for money and ratios, never float — a float price silently corrupts P&L
--   * FK ON DELETE always explicit; RESTRICT unless the child is meaningless without its parent
--   * ix_ / uq_ / fk_ / ck_ naming
--
-- The runner owns the transaction: no BEGIN/COMMIT here (see db/migrate.py).

-- ── shared updated_at trigger ────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION set_updated_at() IS
    'Shared BEFORE UPDATE trigger: maintains updated_at. Attach to every entity table.';

-- ── data sources / provenance ────────────────────────────────────────────────────────────────
-- One row per distinct ingest. Every fact loaded from outside records which pull produced it, so a
-- disagreement between a backtest and a live scan can be traced to a source rather than argued
-- about. Modelled on 9b's corpus_sources.
CREATE TABLE IF NOT EXISTS data_sources (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider     TEXT        NOT NULL,
    dataset      TEXT        NOT NULL,
    -- The point-in-time anchor: when this data was PULLED, which is not the same as the period it
    -- describes. Lookahead audits key off this.
    fetched_at   TIMESTAMPTZ NOT NULL,
    -- Coverage of the payload itself (e.g. the trading day for a minute-bar file).
    period_start DATE,
    period_end   DATE,
    -- Content hash of the source artifact, so a re-ingest of identical bytes is detectable and a
    -- silently-changed upstream file is not.
    source_sha256 TEXT,
    source_uri   TEXT,
    row_count    BIGINT,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_data_sources_provider   CHECK (provider  ~ '^[a-z0-9_]{2,32}$'),
    CONSTRAINT ck_data_sources_dataset    CHECK (dataset   ~ '^[a-z0-9_]{2,64}$'),
    CONSTRAINT ck_data_sources_sha256     CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_data_sources_period     CHECK (period_end IS NULL OR period_start IS NULL OR period_end >= period_start),
    CONSTRAINT ck_data_sources_row_count  CHECK (row_count IS NULL OR row_count >= 0)
);

-- Re-ingesting the identical artifact should be a no-op, not a duplicate. Partial index because
-- source_sha256 is nullable (some sources are streams with no single artifact to hash).
CREATE UNIQUE INDEX IF NOT EXISTS uq_data_sources_artifact
    ON data_sources (provider, dataset, source_sha256)
    WHERE source_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_data_sources_fetched_at ON data_sources (fetched_at DESC);

COMMENT ON TABLE data_sources IS
    'One row per ingest. fetched_at is the point-in-time anchor — the moment the data was pulled, '
    'not the period it describes.';

-- ── securities ───────────────────────────────────────────────────────────────────────────────
-- The reference table. Deliberately includes delisted names: dropping them is exactly how a
-- backtest acquires survivorship bias (SENIOR_ENGINEER_BAR §7.2), so `delisted_at` marks them
-- rather than a DELETE removing them.
CREATE TABLE IF NOT EXISTS securities (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      TEXT        NOT NULL,
    name        TEXT,
    exchange    TEXT,
    sector      TEXT,
    industry    TEXT,
    -- 'cs' common stock, 'etf', 'adrc', … Kept as TEXT + CHECK rather than an enum because the set
    -- grows with whatever the provider returns, and an enum would need a migration per new value.
    security_type TEXT,
    currency    TEXT        NOT NULL DEFAULT 'USD',
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    first_seen  DATE,
    delisted_at DATE,
    source_id   BIGINT      REFERENCES data_sources (id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Ticker grammar: 1-5 uppercase letters with an optional single dotted or hyphenated class
    -- suffix (BRK.B, BRK-B). Deliberately tighter than "letters and dots" so consecutive or
    -- trailing dots cannot enter the reference table.
    CONSTRAINT ck_securities_symbol   CHECK (symbol ~ '^[A-Z]{1,5}([.-][A-Z]{1,2})?$'),
    CONSTRAINT ck_securities_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_securities_delisted CHECK (delisted_at IS NULL OR first_seen IS NULL OR delisted_at >= first_seen)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_securities_symbol ON securities (symbol);
CREATE INDEX IF NOT EXISTS ix_securities_active ON securities (symbol) WHERE is_active;
CREATE INDEX IF NOT EXISTS ix_securities_sector ON securities (sector) WHERE sector IS NOT NULL;

DROP TRIGGER IF EXISTS trg_securities_updated_at ON securities;
CREATE TRIGGER trg_securities_updated_at
    BEFORE UPDATE ON securities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE securities IS
    'Security reference. Delisted names are RETAINED with delisted_at set — removing them is how a '
    'backtest acquires survivorship bias.';
COMMENT ON COLUMN securities.is_active IS
    'Currently tradeable. Never a reason to delete the row; history still references it.';
