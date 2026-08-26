-- 026 — the intraday ratio log (issue #133).
--
-- WHAT IT IS
--   A 30-minute observation of price, and the price-derived ratios that follow from it, for the
--   securities this system actually reasons about: positions held, names under debate, and pipeline
--   proposals. 13 observations per regular session.
--
-- WHY ONLY THE PRICE-DERIVED RATIOS
--   Ratios divide in two. P/E, FCF yield and market cap move with price and are worth 13 rows a day.
--   Margins, ROE, current ratio and debt-to-equity move only when a filing lands — logging those at
--   this cadence writes the identical value 13 times a day, ~3,300 times a year per security. Those
--   already live in fundamentals_snapshots, correctly keyed by known_at, and this table LINKS to
--   that row rather than copying its columns forward.
--
-- WHY A LINEAGE FK AND A FORMULA VERSION
--   `pe_forward` was once mapped from `forwardPriceToEarningsGrowthRatio` — a PEG — and a bear
--   researcher caught it mid-debate, calling it "almost certainly a data error". Had this table
--   been accumulating a computed P/E for months under that mapping, every row would have been
--   quietly wrong.
--
--   So two columns exist purely so that is recoverable:
--     fundamentals_id   the exact statement row the denominators came from. Recompute is possible
--                       because the inputs are identifiable, not merely "whatever was current".
--     formula_version   which arithmetic produced this row. A fix bumps the version, and the rows
--                       computed under the old one can be FOUND and recomputed. Without it, a
--                       corrected table and a wrong one are indistinguishable.
--
--   `database/README.md` reached the lineage half of this independently ("the FK columns encode
--   which Tier 2/3 rows were used to compute the ratios — no values are duplicated"). The version
--   column is the part that was missing.
--
-- ONE FUNDAMENTALS ROW, NOT A FORWARD-FILLED COMPOSITE
--   fundamentals_id names exactly ONE row, and every denominator comes from that row. It is
--   tempting to forward-fill per column — take EPS from whichever earlier row last had one when the
--   in-effect row's is NULL — and it would raise coverage measurably: eps_current is populated on
--   89 of 152 rows, free_cash_flow on 149.
--
--   It is refused because the FK would then be a lie. A P/E whose denominator came from a different
--   vintage than the row this column names is unauditable and unrecomputable, which defeats both
--   columns at once. A NULL ratio beside a populated fundamentals_id is honest: the statement row in
--   effect did not carry that figure.
--
--   Measured consequence today: fcf_yield is broadly computable, pe_trailing is computable for the
--   securities whose in-effect row carries EPS, and pe_forward is structurally NULL for every row
--   because eps_next_year_est is populated on 0 of 152. The column exists anyway — the schema should
--   not need changing when the fundamentals loader starts supplying it.
--
-- WHY A SEPARATE RUNS TABLE
--   Same reasoning as 022 and 024. "The collector never ran", "it ran and every quote failed" and
--   "the market was closed" are three different facts, and a gap in the observations alone cannot
--   tell them apart. A series with holes nobody can attribute is a series nobody can trust.

CREATE TABLE IF NOT EXISTS intraday_collection_runs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_date   date NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT now(),
    completed_at   timestamptz,
    status         text NOT NULL DEFAULT 'running',
    -- How many securities were in scope, and how the fetches went. scope_size = 0 is a real and
    -- reportable outcome (nothing held, nothing debated), NOT a failure.
    scope_size     integer NOT NULL DEFAULT 0,
    observed       integer NOT NULL DEFAULT 0,
    failed         integer NOT NULL DEFAULT 0,
    error          text,

    CONSTRAINT ck_intraday_runs_status CHECK (status = ANY (ARRAY['running','complete','failed','skipped'])),
    CONSTRAINT ck_intraday_runs_counts CHECK (
        scope_size >= 0 AND observed >= 0 AND failed >= 0 AND observed + failed <= scope_size
    ),
    -- A run that has not finished has no completion time, and one that has finished does. Without
    -- this a crashed sweep and a live one are indistinguishable by status alone.
    CONSTRAINT ck_intraday_runs_terminal CHECK (
        (status = 'running' AND completed_at IS NULL)
        OR (status <> 'running' AND completed_at IS NOT NULL)
    ),
    -- A failure explains itself, and 'skipped' does too — "the market was closed" is the single
    -- most common reason this table will hold a non-complete row, and it must say so.
    CONSTRAINT ck_intraday_runs_explained CHECK (
        status NOT IN ('failed','skipped') OR (error IS NOT NULL AND length(btrim(error)) > 0)
    )
);

CREATE INDEX IF NOT EXISTS ix_intraday_runs_session ON intraday_collection_runs (session_date DESC, started_at DESC);

CREATE TABLE IF NOT EXISTS intraday_observations (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint REFERENCES intraday_collection_runs (id) ON DELETE SET NULL,
    security_id     bigint NOT NULL REFERENCES securities (id) ON DELETE RESTRICT,
    observed_at     timestamptz NOT NULL,
    session_date    date NOT NULL,

    -- WHY this security was collected. Without it, a security disappearing from the series is
    -- ambiguous between "the collector stopped" and "it left the watchlist" — the same ambiguity
    -- 024 removed for the cycle and 025 for gap dispositions. Do not let it back in.
    scope_reasons   text[] NOT NULL,

    price           numeric(18,6) NOT NULL,
    market_cap      numeric(24,2),
    volume          bigint,

    -- Price-derived only. Everything here has price in the numerator; the denominators come from
    -- the fundamentals row named below.
    pe_trailing     numeric(18,6),
    pe_forward      numeric(18,6),
    fcf_yield       numeric(18,6),

    -- Lineage. NULL means no statement row was in effect at observation time, which is why the
    -- ratios above are then NULL too — see ck_intraday_obs_ratios_have_lineage.
    fundamentals_id bigint REFERENCES fundamentals_snapshots (id) ON DELETE RESTRICT,
    -- Which arithmetic produced the ratios. No default: a row that cannot say how it was computed
    -- cannot be corrected later, and correcting later is the entire point of this column.
    formula_version integer NOT NULL,

    source          text NOT NULL DEFAULT 'fmp',
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_intraday_obs_price CHECK (price > 0),
    -- cardinality(), NOT array_length(). array_length('{}', 1) returns NULL rather than 0, and a
    -- CHECK only fails on FALSE — so `array_length(...) >= 1` evaluates to NULL for an empty array
    -- and the row is ACCEPTED. The first draft here did exactly that, and an observation with no
    -- recorded reason would have sailed in: precisely the ambiguity this column exists to prevent.
    CONSTRAINT ck_intraday_obs_scope CHECK (
        cardinality(scope_reasons) >= 1
        AND scope_reasons <@ ARRAY['held','debated','proposed','slate']
    ),
    -- A ratio computed from no statement row was computed from nothing. This is the constraint that
    -- stops a placeholder denominator from ever being written as a measurement — the same defect
    -- class as the 0.5 the ML library used to return for "we could not measure this".
    CONSTRAINT ck_intraday_obs_ratios_have_lineage CHECK (
        fundamentals_id IS NOT NULL
        OR (pe_trailing IS NULL AND pe_forward IS NULL AND fcf_yield IS NULL)
    ),
    CONSTRAINT ck_intraday_obs_formula CHECK (formula_version >= 1),
    -- One observation per security per instant. A re-run of the same 30-minute slot updates rather
    -- than doubling the series.
    CONSTRAINT uq_intraday_obs UNIQUE (security_id, observed_at)
);

CREATE INDEX IF NOT EXISTS ix_intraday_obs_security_time ON intraday_observations (security_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_intraday_obs_session ON intraday_observations (session_date DESC);
CREATE INDEX IF NOT EXISTS ix_intraday_obs_run ON intraday_observations (run_id);
-- Finding every row computed under a superseded formula is the query this design exists to make
-- possible; it should not be a sequential scan over the whole series.
CREATE INDEX IF NOT EXISTS ix_intraday_obs_formula ON intraday_observations (formula_version);

COMMENT ON TABLE intraday_observations IS
    'Issue #133. 30-minute price observations and PRICE-DERIVED ratios for held/debated/proposed '
    'securities. Statement-derived ratios (margins, ROE, leverage) are NOT here — they change '
    'quarterly and live in fundamentals_snapshots, which fundamentals_id points at. formula_version '
    'identifies the arithmetic so a corrected formula can be applied retroactively.';

-- ── correcting 025's investable set ───────────────────────────────────────────────────────────
--
-- 025 defined the investable universe as ('stock','etf') and excluded share classes. That was
-- wrong, and it was caught the first time the collector below resolved its scope: 14 of 15 held
-- names, and the missing one was BRK.B. Berkshire B, Brown-Forman B, HEICO A and Crawford A are
-- ordinary investable shares of ordinary companies — the distinct TYPE is worth keeping, the
-- investability call was not.
--
-- Rebuilt rather than left stale: an index predicate that disagrees with
-- instrument_class.INVESTABLE is a universe filter with two definitions, and the whole point of
-- putting is_investable in one place was to stop that.
DROP INDEX IF EXISTS ix_securities_investable;
CREATE INDEX IF NOT EXISTS ix_securities_investable ON securities (symbol)
    WHERE security_type IN ('stock', 'etf', 'share_class');

COMMENT ON COLUMN securities.security_type IS
    'Instrument form: stock | etf | warrant | unit | right | share_class | untracked. '
    'NULL = never classified, which is NOT the same as untracked (the provider does not carry it). '
    'Populated by db/load_instrument_types.py from two bulk FMP lists plus symbol convention; '
    'db/instrument_class.py::is_investable is the single definition of what a universe may draw '
    'from, and it admits stock, etf AND share_class. See issue #41.';

GRANT SELECT, INSERT, UPDATE ON intraday_observations, intraday_collection_runs TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE intraday_observations_id_seq, intraday_collection_runs_id_seq TO rh_app;
