-- 001_core_schema — securities, data-source provenance, the shared updated_at trigger, and the
-- least-privilege runtime role.
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
--   * FK ON DELETE always explicit, and every FK constraint is NAMED (fk_…) — auto-generated
--     names diverge across environments and break migration diffs
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
    'Shared BEFORE UPDATE trigger: maintains updated_at. Attach to every mutable entity table.';

-- ── runtime role (least privilege, SENIOR_ENGINEER_BAR §4.9) ─────────────────────────────────
-- The migration/DDL role is the container superuser (`rh`); the future app connects as `rh_app`,
-- which gets DML only — no DDL, no superuser, and therefore no `COPY … FROM PROGRAM`. Created
-- with NO password: it cannot authenticate until an operator sets one
-- (bin/db_psql.sh -c "ALTER ROLE rh_app WITH PASSWORD '…'"), so shipping the role early adds no
-- attack surface. CREATE ROLE has no IF NOT EXISTS in PG16, hence the catalog check.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rh_app') THEN
        CREATE ROLE rh_app LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO rh_app;
-- Future tables/sequences created by the migration role inherit these automatically, so 002+ (and
-- the evaluation tables to come) need no per-migration grant boilerplate. schema_migrations
-- predates this statement and is deliberately NOT granted — the app has no business writing
-- migration bookkeeping.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rh_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rh_app;

-- ── data sources / provenance ────────────────────────────────────────────────────────────────
-- One row per distinct ingest. Every fact loaded from outside records which pull produced it, so a
-- disagreement between a backtest and a live scan can be traced to a source rather than argued
-- about. Modelled on 9b's corpus_sources.
--
-- APPEND-ONLY BY POLICY: rows here are the provenance record and are never deleted — every
-- source_id FK in this schema is ON DELETE RESTRICT, so a DELETE against a referenced row is
-- refused. (Deliberate deviation from Bar §4.1 "index every FK": source_id is deliberately
-- unindexed on the two bar tables — see 002 — because with an append-only parent the only query
-- an index would serve is the refused DELETE, and the index would cost tens of GB at 1.6B rows.)
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
    -- row_count and notes are legitimately corrected after a load completes; updated_at shows it
    -- happened (Bar §4.3).
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

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

DROP TRIGGER IF EXISTS trg_data_sources_updated_at ON data_sources;
CREATE TRIGGER trg_data_sources_updated_at
    BEFORE UPDATE ON data_sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE data_sources IS
    'One row per ingest, append-only (all source_id FKs are RESTRICT). fetched_at is the '
    'point-in-time anchor — the moment the data was pulled, not the period it describes.';

-- ── securities ───────────────────────────────────────────────────────────────────────────────
-- The reference table. Deliberately includes delisted names: dropping them is exactly how a
-- backtest acquires survivorship bias (SENIOR_ENGINEER_BAR §7.2), so `delisted_at` marks them
-- rather than a DELETE removing them. "Active" is DEFINED as delisted_at IS NULL — there is no
-- separate is_active flag, because two columns encoding one truth is how reference data rots.
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
    -- Earliest date this row's identity is known to have been listed. NULL means "listed before
    -- our data begins / not yet populated" — the loader sets it from the earliest observation and
    -- point-in-time universe queries must treat NULL as unknown-start, not as never-listed.
    first_seen  DATE,
    delisted_at DATE,
    source_id   BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_securities_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,

    -- Symbol grammar: the canonical form is the Polygon flat-file ticker, stored VERBATIM —
    -- uppercase root, optional lowercase class markers (p = preferred, r = rights, w = warrant,
    -- e.g. BACpA, AANw), optional dotted suffixes (BRK.B, TDW.WS.A). Loaders from other providers
    -- NORMALIZE to this form (BRK-B → BRK.B; Bloomberg 'NVDA US Equity' → NVDA); a symbol a loader
    -- chooses to skip instead must be a logged decision, not a swallowed exception.
    -- Validated empirically against real day files (2020-11-30, 2021-06-30, 2023-06-30 —
    -- 12,928 distinct tickers, 0 rejected; the previous 1-5-uppercase grammar rejected 482 real
    -- symbols in the 2020-11-30 file alone).
    CONSTRAINT ck_securities_symbol   CHECK (symbol ~ '^[A-Za-z][A-Za-z0-9]{0,9}(\.[A-Za-z0-9]{1,4}){0,2}$'),
    CONSTRAINT ck_securities_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_securities_delisted CHECK (delisted_at IS NULL OR first_seen IS NULL OR delisted_at >= first_seen)
);

-- One LIVE holder per symbol, not one holder ever: tickers get recycled across a 5-year universe,
-- and a global unique would force a re-listed symbol to overwrite the delisted company's identity
-- (survivorship bias's uglier sibling). Loader rule: a symbol re-appearing after its previous
-- holder was delisted is a NEW row; historical symbol→id resolution goes through
-- (symbol, as-of-date) against first_seen/delisted_at.
CREATE UNIQUE INDEX IF NOT EXISTS uq_securities_symbol_live
    ON securities (symbol) WHERE delisted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_securities_symbol ON securities (symbol);
CREATE INDEX IF NOT EXISTS ix_securities_sector ON securities (sector) WHERE sector IS NOT NULL;

DROP TRIGGER IF EXISTS trg_securities_updated_at ON securities;
CREATE TRIGGER trg_securities_updated_at
    BEFORE UPDATE ON securities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE securities IS
    'Security reference. Delisted names are RETAINED with delisted_at set — removing them is how a '
    'backtest acquires survivorship bias. Active = delisted_at IS NULL; a recycled ticker is a new row.';
COMMENT ON COLUMN securities.first_seen IS
    'Earliest known listing date. NULL = listed before our data begins / not yet populated — treat '
    'as unknown-start in point-in-time universe queries, never as never-listed.';
