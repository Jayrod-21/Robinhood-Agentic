-- 007_point_in_time — bound the split adjustment to a declared as-of date, make the evaluation
-- schema state its return basis / rf convention / session coverage, and correct catalog comments
-- that documented untrue things.
--
-- Issued by the Phase A fix-pass (docs/fixpass/REVIEW_phaseA_loaders.md,
-- REVIEW_phaseA_semantics.md). 005/006 are APPLIED and checksummed, so their corrections land
-- here as new DDL and re-issued COMMENT ONs — never by editing the applied files (that raises
-- ChecksumMismatch by design).
--
-- WHY THE AS-OF BOUND (B-S1 — lookahead)
--   005's split_factor_after(security_id, date) multiplied EVERY split with ex_date > date, with
--   no upper bound. The provider returns splits through today; the archive ends 2025-10-02.
--   Measured: 527 splits across 436 securities with ex_date after the archive window put
--   post-decision information into 308,709 bars' stored price LEVELS — PAVS closed $1.04 on
--   2025-09-30 with a stored adj_close of $124,800 (its 1-for-120 reverse splits happened in
--   Dec 2025 – Jun 2026), and 196,909 bars crossed a $5 price floor on adj_close that they fail
--   on close. EVALUATION_FRAMEWORK §3.5 requires this class of error to be structurally
--   impossible.
--
--   The asymmetry that shapes the fix: RETURNS were never contaminated. factor(t-1)/factor(t)
--   equals the product of splits with ex_date in (t-1, t] — any split after both endpoints
--   cancels — so a return at date t only ever embeds splits observable at t. It is the LEVEL that
--   is poisoned by future splits. Therefore:
--
--     * split_factor_between(security_id, bar_date, as_of) replaces the unbounded function: the
--       product of splits with ex_date in (bar_date, as_of]. The unbounded split_factor_after is
--       DROPPED — misuse becomes impossible, not discouraged.
--     * The materialised columns are pinned to a DECLARED adjustment_as_of (the archive's last
--       covered session), recorded in price_adjustment_state by the adjust pass. Stored factors
--       make returns point-in-time safe at every date. Stored adj_close LEVELS are safe only for
--       decisions at or after adjustment_as_of; a screen at a historical decision date T must use
--       close ÷ split_factor_between(security_id, trade_date, T).
--
--   ON announced_at (005 forbids point-in-time use of rows with NULL announced_at, and 100% of
--   46,934 rows are NULL): no contradiction remains, and here is the reasoning rather than an
--   assertion. A split is a public, market-observable event ON its ex-date — the price trades on
--   the new basis that morning. Bounding by ex_date <= as_of therefore uses only events that had
--   already publicly HAPPENED by as_of, which needs no announcement timestamp. announced_at
--   matters only for anticipating an action BEFORE its ex-date (e.g. trading the announcement),
--   which nothing in this schema does. 005's rule stands for that use; the adjustment never
--   needed it.
--
-- WHY return_basis (B-S2 — a price-only number in a column named total_return)
--   Dividends exist for 5.0% of securities (gap-based candidate selection finds splits, not
--   dividends; SPY has zero dividend rows), so the marking job has nothing to credit and computes
--   a PRICE-ONLY return — a 1.6–9.1 pp/yr strategy-correlated understatement (SPY 1.62, T 7.57,
--   MO 9.05). Until dividend coverage is real, the schema must refuse to let a price-only figure
--   masquerade as a total return: return_basis is NOT NULL with no default, so every writer
--   states which number it computed.
--
-- WHY rf_conversion (S-S2) — the same nine returns produce Sharpe 0.7747 (rf/252) or 0.8301
--   (rf/365); the stored rf/periods_per_year did not pin the conversion rule, so two rows were
--   not actually comparable. Now the rule is a column.
--
-- WHY expected_sessions / coverage_ratio (S-S1) — n_observations was verified against the marks
--   that EXIST, and bounded by CALENDAR days; neither can see a 15-session hole (the December-2024
--   gap costs SPY 5.4% of Sharpe and passed both checks). expected_sessions is verified by
--   trigger against market_calendar, and coverage_ratio makes a shortfall a stored, queryable
--   fact instead of a silent one.
--
-- migrate: filename carries no destructive marker — this migration creates, adds columns, and
-- re-issues comments; it drops only the lookahead-unsafe FUNCTION (recreated by the down).

-- ── the adjustment state ─────────────────────────────────────────────────────────────────────
-- Single row: the declared upper bound of the materialised adjustment. Written by the adjust
-- pass BEFORE it writes factors, so a factor column can never exist without its declared as-of.
CREATE TABLE IF NOT EXISTS price_adjustment_state (
    id               SMALLINT    PRIMARY KEY,
    adjustment_as_of DATE        NOT NULL,
    adjusted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A singleton by construction, not convention.
    CONSTRAINT ck_price_adjustment_state_singleton CHECK (id = 1)
);

DROP TRIGGER IF EXISTS trg_price_adjustment_state_updated_at ON price_adjustment_state;
CREATE TRIGGER trg_price_adjustment_state_updated_at
    BEFORE UPDATE ON price_adjustment_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE price_adjustment_state IS
    'Singleton: the as-of date the materialised split adjustment is pinned to (the archive''s '
    'last covered session when adjust ran). Stored factors embed NO split with ex_date after '
    'this date. Levels (adj_close) are only decision-safe at or after it; earlier decision dates '
    'must use split_factor_between(security_id, trade_date, decision_date).';

-- ── the bounded factor ───────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION split_factor_between(p_security_id BIGINT, p_date DATE, p_as_of DATE)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    -- Product of split ratios with ex_date in (p_date, p_as_of]. COALESCE to 1 when none: the
    -- price then needs no adjustment. ROUNDed because exp(sum(ln(x))) goes through double
    -- precision and does not round-trip (a single 4-for-1 returns 3.9999999999999999); 12
    -- decimal places is exact for real split ratios — simple rationals like 4, 1.5, 0.1 —
    -- while removing the artefact. Postgres has no aggregate product, hence the log identity.
    -- (Same numeric reasoning as 005's split_factor_after, which this replaces; the ONLY
    -- semantic change is the upper bound.)
    SELECT ROUND(COALESCE(
        (SELECT exp(sum(ln(split_ratio)))
         FROM corporate_actions
         WHERE security_id = p_security_id
           AND action_type = 'split'
           AND ex_date > p_date
           AND ex_date <= p_as_of),
        1
    ), 12);
$$;

COMMENT ON FUNCTION split_factor_between(BIGINT, DATE, DATE) IS
    'Product of split ratios with ex_date in (p_date, p_as_of]. Divide a raw close by this to '
    'get the split-adjusted close AS KNOWABLE AT p_as_of. Returns 1 when no split lies in the '
    'interval. Replaces the unbounded split_factor_after, whose missing upper bound let '
    'post-archive splits contaminate historical price levels (lookahead).';

-- The unbounded function is REMOVED, not deprecated: with it gone, recomputing a factor without
-- stating an as-of is impossible rather than discouraged. (DROP FUNCTION destroys no data.)
DROP FUNCTION IF EXISTS split_factor_after(BIGINT, DATE);

-- ── evaluation_runs: state what the number is ────────────────────────────────────────────────
-- The table is empty (verified before this migration was written; NOT NULL without DEFAULT is a
-- deliberate writer-must-state contract, and would loudly refuse to apply over existing rows).
ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS return_basis TEXT NOT NULL;
ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS rf_conversion TEXT NOT NULL;
ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS expected_sessions INTEGER NOT NULL;
-- Stored, generated: a coverage shortfall is a queryable fact on the row itself, not a join
-- someone must remember to run. expected_sessions >= 1 is CHECKed below, so no division by zero.
-- Unconstrained NUMERIC deliberately: generated columns are computed BEFORE check constraints
-- run, so a precision-capped type would turn an absurd-n row's rejection into a confusing
-- "numeric field overflow" instead of the named CHECK below (observed in the test suite).
ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS coverage_ratio NUMERIC
        GENERATED ALWAYS AS (round(n_observations::numeric / expected_sessions, 6)) STORED;

ALTER TABLE evaluation_runs
    ADD CONSTRAINT ck_evaluation_runs_return_basis
        CHECK (return_basis IN ('price_only', 'total_return'));
-- 'simple'   — per-period rf = risk_free_annual / periods_per_year
-- 'compound' — per-period rf = (1 + risk_free_annual)^(1/periods_per_year) - 1
-- 'annual'   — rf subtracted at the annual level after annualising the mean return
ALTER TABLE evaluation_runs
    ADD CONSTRAINT ck_evaluation_runs_rf_conversion
        CHECK (rf_conversion IN ('simple', 'compound', 'annual'));
ALTER TABLE evaluation_runs
    ADD CONSTRAINT ck_evaluation_runs_expected_sessions
        CHECK (expected_sessions >= 1 AND n_observations <= expected_sessions);

COMMENT ON COLUMN evaluation_runs.return_basis IS
    'What the return columns actually are: price_only (no dividend cash credited — the current '
    'state for ~95% of the universe) or total_return. NOT NULL with no default: a price-only '
    'Sharpe stored as a total-return one is a 1.6-9.1 pp/yr strategy-correlated lie.';
COMMENT ON COLUMN evaluation_runs.rf_conversion IS
    'How risk_free_annual became a per-period rate: simple = rf/periods_per_year; compound = '
    '(1+rf)^(1/ppy)-1; annual = subtracted after annualising. The /252-vs-/365 choice alone moves '
    'a Sharpe 7% — without this column two rows were not comparable.';
COMMENT ON COLUMN evaluation_runs.expected_sessions IS
    'Trading sessions in [window_start, window_end] per market_calendar — VERIFIED by '
    'trg_evaluation_runs_n against the calendar, so it cannot be asserted wrong. With '
    'n_observations this makes a coverage hole (e.g. the 15-session December-2024 gap) visible '
    'on the row.';
COMMENT ON COLUMN evaluation_runs.coverage_ratio IS
    'n_observations / expected_sessions, generated. 1.0 = every session marked. A Sharpe at '
    'coverage 0.9 spans holes whose gap-returns are multi-session moves wearing daily clothes '
    '(measured: the Dec-2024 hole alone moves SPY''s five-year Sharpe 5.4% relative).';

-- n_observations verification now covers BOTH directions: the claimed n against the marks that
-- exist (004's original check), and the claimed expected_sessions against the calendar. A window
-- with no calendar coverage is refused outright — a metric over dates the calendar cannot
-- describe is unauditable. (Replaces 004's function; CREATE OR REPLACE keeps the trigger wired.)
CREATE OR REPLACE FUNCTION enforce_eval_run_n_observations() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_actual   INTEGER;
    v_sessions INTEGER;
BEGIN
    IF NEW.return_frequency = 'daily' THEN
        SELECT count(*) INTO v_actual
        FROM portfolio_returns_daily
        WHERE portfolio_id = NEW.portfolio_id
          AND trade_date BETWEEN NEW.window_start AND NEW.window_end
          AND daily_return IS NOT NULL;
        IF v_actual <> NEW.n_observations THEN
            RAISE EXCEPTION 'evaluation run claims n_observations = % but portfolio % has % marks with returns in [%, %]',
                NEW.n_observations, NEW.portfolio_id, v_actual, NEW.window_start, NEW.window_end
                USING ERRCODE = 'check_violation';
        END IF;

        SELECT count(*) INTO v_sessions
        FROM market_calendar
        WHERE is_trading_day AND trade_date BETWEEN NEW.window_start AND NEW.window_end;
        IF v_sessions = 0 THEN
            RAISE EXCEPTION 'market_calendar has no trading days in [%, %] — load the calendar before storing metrics for this window',
                NEW.window_start, NEW.window_end
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.expected_sessions <> v_sessions THEN
            RAISE EXCEPTION 'evaluation run claims expected_sessions = % but market_calendar has % trading days in [%, %]',
                NEW.expected_sessions, v_sessions, NEW.window_start, NEW.window_end
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

-- ── comment corrections: say true things in the catalog ──────────────────────────────────────

-- B-S4: the stored close is NOT the official close. The official close is the closing-auction
-- print, which lands inside the excluded 16:00 ET minute bucket at a non-fixed position, so it
-- cannot be recovered from 1-minute aggregates. Do not "fix" this by moving the session bound to
-- 16:00 — that bucket's close is a post-close print, a different wrong number. The fix is a
-- source that carries the official close (e.g. Polygon daily aggregates).
COMMENT ON COLUMN price_bars_daily.close IS
    'Close of the 15:59 ET minute bar — the last regular-session minute — NOT the official '
    'closing-auction print, which lands in the excluded 16:00 bucket and is not recoverable from '
    'minute aggregates. Measured vs official (SPY, 1,241 sessions): 6.8% match to half a cent, '
    'worst 95.7 bps, mean −0.165 bps (no systematic bias). open DOES match the official open (the '
    '09:30 bucket captures the opening auction); 13:00 early-close days DO capture the closing '
    'auction. Reconciliation against official daily data is expected to differ within these '
    'bounds until an official-close source is loaded.';
COMMENT ON COLUMN price_bars_daily.volume IS
    'Regular-session (09:30-15:59 ET) share volume: excludes extended hours AND the closing '
    'auction, so it runs ~15% below official daily volume (SPY median ours/official 0.853). Any '
    'ADV, participation-rate, or slippage model calibrated on this understates liquidity.';

-- B-S1/S-S9: adj_close and split_adj_factor, re-stated with the as-of pin and the real NULL rule.
COMMENT ON COLUMN price_bars_daily.split_adj_factor IS
    'Product of split ratios with ex_date in (trade_date, adjustment_as_of] — see '
    'price_adjustment_state; 1 when none. THE stable representation: returns are (close(t)/f(t)) '
    '÷ (close(t-1)/f(t-1)), point-in-time safe at every t because the ratio only embeds splits '
    'with ex_date <= t. Always populated once adjust has run (adjust exits non-zero while any '
    'factor is NULL).';
COMMENT ON COLUMN price_bars_daily.adj_close IS
    'SPLIT-adjusted close = close ÷ split_adj_factor, pinned to adjustment_as_of '
    '(price_adjustment_state). NOT dividend-adjusted: dividends become cash at marking, and '
    'dividend coverage is ~5% of securities — treat any derived return as price_only until that '
    'changes (evaluation_runs.return_basis). LEVEL screens for a decision at historical time T '
    'MUST NOT use this column (it embeds splits between T and adjustment_as_of): use close ÷ '
    'split_factor_between(security_id, trade_date, T). NULL means the level exceeds $1e12/share '
    '(serial reverse-splitters) and is meaningless — use the factor; treat NULL as an error, '
    'never as zero. NEVER a marking price: fills and marks are raw-close domain.';

-- B-S3: the documented marking formula mixed share bases — Σ shares × adj_close is wrong by the
-- split factor for any lot held across a split (40× for a pre-2021 NVDA lot), because adj_close
-- is on the CURRENT share basis while lot shares are as-traded. Marking is raw × raw: keep lot
-- share counts on the current basis by applying each split on its ex-date, and mark at the raw
-- close.
COMMENT ON TABLE paper_portfolio_positions IS
    'Lot-level holdings per portfolio: entry, exit, realized P&L. Makes market_value recomputable '
    'as Σ shares × RAW close + cash, and gives EVALUATION_FRAMEWORK §3.1 (entry thesis → exit → '
    'lesson) its per-position record. SPLIT RULE: on a split ex-date the marking job multiplies '
    'shares by split_ratio and divides entry_price by it (as-traded basis throughout). NEVER mark '
    'with adj_close — it is on the current share basis and mixing bases mis-marks any lot held '
    'across a split by the split factor. Entries are immutable to the runtime role; only the exit '
    'columns are updatable.';
COMMENT ON COLUMN paper_portfolios.cash IS
    'Current cash balance maintained by the marking job; set to base_value at inception. With '
    'paper_portfolio_positions this makes market_value recomputable (Σ shares × RAW close + '
    'cash — never adj_close; see paper_portfolio_positions) rather than trusted. Dividends are '
    'credited HERE on their ex-date (cash accounting) — which is exactly why prices must not '
    'also be dividend-adjusted.';

-- announced_at: resolve 005's apparent contradiction (see header — ex-date bounding needs no
-- announcement time; announced_at exists for pre-ex-date anticipation, which nothing does yet).
COMMENT ON COLUMN corporate_actions.announced_at IS
    'When the action became publicly known, when the provider supplies it (currently NULL on all '
    'yfinance rows). NOT required for split adjustment: a split is market-observable ON its '
    'ex-date, and factors are bounded by ex_date <= as_of, so the adjustment uses only events '
    'that had already happened. Required only for a strategy that wants to act BETWEEN '
    'announcement and ex-date — such a use must exclude NULL rows (005''s rule, which stands).';

-- S-S7: first_seen left-censoring is a fact of THIS archive, not just a NULL edge case.
COMMENT ON COLUMN securities.first_seen IS
    'Earliest known listing date. NULL = unknown-start. LEFT-CENSORED at the archive start: '
    '8,678 securities carry 2020-10-02 (the archive''s first day), which is when our DATA begins, '
    'not when they listed. Never build listing-age/IPO features from this column for rows at the '
    'censoring boundary. name/exchange/security_type/sector/industry are currently unpopulated '
    '(no reference-metadata feed loaded): universe queries cannot yet exclude warrants/rights/'
    'preferreds except by symbol-form heuristics.';
