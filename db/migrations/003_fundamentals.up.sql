-- 003_fundamentals — dated fundamentals snapshots, point-in-time by construction.
--
-- migrate: non-destructive
--
-- The Sprinkle Sauce screen reads this table. Two dates are stored and they are NOT the same:
--
--   period_end  — the fiscal period the numbers describe
--   known_at    — when the numbers became publicly available (filing/acceptance date)
--
-- Every point-in-time query filters on known_at, never period_end. Screening a 2021-Q1 figure on
-- 2021-04-01 is lookahead if the 10-Q was not filed until 2021-05-05, and that single confusion is
-- the most common way a backtest reports an edge it does not have.
--
-- The distinction also decides what we can honestly claim from an FMP purchase: if their historical
-- fundamentals are as-currently-restated rather than as-first-reported, known_at is unrecoverable
-- and every backtest over them inherits lookahead. That question is on the pre-purchase checklist.

CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id  BIGINT      NOT NULL REFERENCES securities (id) ON DELETE RESTRICT,

    period_end   DATE        NOT NULL,
    -- 'annual' | 'quarterly' | 'ttm' | 'snapshot'. TEXT + CHECK rather than an enum: adding a
    -- period type to an enum needs a migration, and PG16 has no CREATE TYPE IF NOT EXISTS.
    period_type  TEXT        NOT NULL,
    -- NULL means "filing date unknown" — such rows MUST be excluded from point-in-time backtests
    -- rather than assumed available at period_end. Nullable on purpose so the gap is visible.
    known_at     TIMESTAMPTZ,

    -- ── valuation ──
    market_cap        NUMERIC(24, 2),
    price             NUMERIC(18, 6),
    pe_trailing       NUMERIC(18, 6),
    pe_forward        NUMERIC(18, 6),
    peg_ratio         NUMERIC(18, 6),
    eps_current       NUMERIC(18, 6),
    eps_next_year_est NUMERIC(18, 6),

    -- ── cash flow / quality ──
    free_cash_flow    NUMERIC(24, 2),
    fcf_yield         NUMERIC(18, 6),
    ebitda_margin     NUMERIC(18, 6),
    gross_margin      NUMERIC(18, 6),
    operating_margin  NUMERIC(18, 6),
    net_margin        NUMERIC(18, 6),
    roe               NUMERIC(18, 6),
    roc               NUMERIC(18, 6),

    -- ── balance sheet / solvency ──
    current_ratio     NUMERIC(18, 6),
    quick_ratio       NUMERIC(18, 6),
    debt_to_equity    NUMERIC(18, 6),
    ebitda_interest   NUMERIC(18, 6),

    -- ── growth / other ──
    revenue_growth_yoy      NUMERIC(18, 6),
    cash_conversion_cycle   NUMERIC(18, 6),
    short_interest          NUMERIC(18, 6),
    -- 0-9. The screen currently approximates this from yfinance; Bloomberg and FMP full
    -- fundamentals supply the real thing.
    piotroski_f_score       SMALLINT,

    -- Provider fields we have not modelled as columns. JSONB, not JSON: JSON stores the raw text and
    -- reparses on every read.
    extra        JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Cells the provider returned that could not be parsed as numbers, kept verbatim as
    -- {column: original_string}. The Bloomberg export contains '#N/A Invalid Field', '#VALUE!', and
    -- '#N/A N/A'; a naive float() either throws or coerces them to something wrong. Storing NULL in
    -- the typed column AND the original string here keeps "we do not know" distinguishable from
    -- "the provider said this and we could not read it".
    unparsed     JSONB       NOT NULL DEFAULT '{}'::jsonb,

    source_id    BIGINT      REFERENCES data_sources (id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_fundamentals_period_type CHECK (period_type IN ('annual', 'quarterly', 'ttm', 'snapshot')),
    CONSTRAINT ck_fundamentals_piotroski   CHECK (piotroski_f_score IS NULL OR piotroski_f_score BETWEEN 0 AND 9),
    CONSTRAINT ck_fundamentals_market_cap  CHECK (market_cap IS NULL OR market_cap >= 0),
    CONSTRAINT ck_fundamentals_price       CHECK (price IS NULL OR price > 0),
    -- known_at before the period it describes would be a data error, not merely odd.
    CONSTRAINT ck_fundamentals_known_at    CHECK (known_at IS NULL OR known_at >= period_end::timestamptz),
    CONSTRAINT ck_fundamentals_extra_obj    CHECK (jsonb_typeof(extra) = 'object'),
    CONSTRAINT ck_fundamentals_unparsed_obj CHECK (jsonb_typeof(unparsed) = 'object')
);

-- One snapshot per security/period/type/source. Source is part of the key on purpose: Bloomberg and
-- FMP will disagree about the same quarter, and both readings are worth keeping so the disagreement
-- is inspectable rather than resolved by whichever loaded last.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fundamentals_snapshot
    ON fundamentals_snapshots (security_id, period_end, period_type, COALESCE(source_id, 0));

-- The point-in-time lookup: "latest row for this security known by time T".
CREATE INDEX IF NOT EXISTS ix_fundamentals_pit
    ON fundamentals_snapshots (security_id, known_at DESC)
    WHERE known_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_fundamentals_period ON fundamentals_snapshots (period_end DESC);

-- Screening runs filter hard on these two, and both are highly selective.
CREATE INDEX IF NOT EXISTS ix_fundamentals_screen
    ON fundamentals_snapshots (peg_ratio, fcf_yield)
    WHERE peg_ratio IS NOT NULL AND fcf_yield IS NOT NULL;

DROP TRIGGER IF EXISTS trg_fundamentals_updated_at ON fundamentals_snapshots;
CREATE TRIGGER trg_fundamentals_updated_at
    BEFORE UPDATE ON fundamentals_snapshots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE fundamentals_snapshots IS
    'Dated fundamentals. Point-in-time queries filter on known_at (when it was publicly available), '
    'never period_end (what it describes).';
COMMENT ON COLUMN fundamentals_snapshots.known_at IS
    'Filing/acceptance date. NULL = unknown; such rows must be EXCLUDED from point-in-time '
    'backtests, not assumed available at period_end.';
COMMENT ON COLUMN fundamentals_snapshots.unparsed IS
    'Provider cells that would not parse, kept verbatim (e.g. Bloomberg #N/A Invalid Field, #VALUE!). '
    'Typed column is NULL; this preserves what was actually returned.';
