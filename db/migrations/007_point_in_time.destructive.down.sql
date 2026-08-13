-- 007_point_in_time (down) — remove the as-of state and the evaluation columns, restore the
-- unbounded factor function and the pre-007 comments.
--
-- migrate: DESTRUCTIVE — drops price_adjustment_state (the declared adjustment bound) and four
-- evaluation_runs columns. evaluation_runs is expected empty at rollback time (007 added NOT
-- NULL columns to an empty table); price_adjustment_state's single row is recomputable by
-- re-running the adjust pass. NOTE: restoring split_factor_after restores 005's lookahead-unsafe
-- behaviour by definition — that is what rolling 007 back MEANS.

-- Restore 005's unbounded function verbatim (body and comment from 005_corporate_actions.up.sql).
CREATE OR REPLACE FUNCTION split_factor_after(p_security_id BIGINT, p_date DATE)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    SELECT ROUND(COALESCE(
        (SELECT exp(sum(ln(split_ratio)))
         FROM corporate_actions
         WHERE security_id = p_security_id
           AND action_type = 'split'
           AND ex_date > p_date),
        1
    ), 12);
$$;

COMMENT ON FUNCTION split_factor_after(BIGINT, DATE) IS
    'Product of every split ratio taking effect AFTER p_date. Divide a raw close by this to get the '
    'split-adjusted close. Returns 1 when no later split exists. Computed as exp(sum(ln)) because '
    'Postgres has no aggregate product; every ratio is > 0 by CHECK, so ln() is always defined.';

DROP FUNCTION IF EXISTS split_factor_between(BIGINT, DATE, DATE);
DROP TABLE IF EXISTS price_adjustment_state;

-- coverage_ratio depends on expected_sessions, so it goes first; dropping the columns drops
-- their CHECK constraints with them.
ALTER TABLE evaluation_runs DROP COLUMN IF EXISTS coverage_ratio;
ALTER TABLE evaluation_runs DROP COLUMN IF EXISTS expected_sessions;
ALTER TABLE evaluation_runs DROP COLUMN IF EXISTS rf_conversion;
ALTER TABLE evaluation_runs DROP COLUMN IF EXISTS return_basis;

-- Restore 004's verification function verbatim (marks check only).
CREATE OR REPLACE FUNCTION enforce_eval_run_n_observations() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_actual INTEGER;
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
    END IF;
    RETURN NULL;
END;
$$;

-- Restore pre-007 comments. close/volume/announced_at had none before 007.
COMMENT ON COLUMN price_bars_daily.close IS NULL;
COMMENT ON COLUMN price_bars_daily.volume IS NULL;
COMMENT ON COLUMN corporate_actions.announced_at IS NULL;

-- 006's texts, verbatim.
COMMENT ON COLUMN price_bars_daily.split_adj_factor IS
    'Cumulative product of split ratios taking effect AFTER trade_date; 1 when none. THE stable '
    'representation — returns are (close(t)/f(t)) ÷ (close(t-1)/f(t-1)), where the large '
    'intermediate cancels. Always populated once the adjustment has run.';
COMMENT ON COLUMN price_bars_daily.adj_close IS
    'SPLIT-adjusted close = close ÷ split_adj_factor, widened to NUMERIC(30,10). NOT '
    'dividend-adjusted: dividends become cash at marking, so adjusting the price too would '
    'double-count. NULL means the level is not representable (serial reverse-splitters reach ~1e13) '
    '— use split_adj_factor instead. A consumer that needs a level MUST treat NULL as an error, '
    'never as zero.';

-- 004's texts, verbatim.
COMMENT ON TABLE paper_portfolio_positions IS
    'Lot-level holdings per portfolio: entry, exit, realized P&L. Makes market_value recomputable '
    'and gives EVALUATION_FRAMEWORK §3.1 (entry thesis → exit → lesson) its per-position record. '
    'Entries are immutable to the runtime role; only the exit columns are updatable.';
COMMENT ON COLUMN paper_portfolios.cash IS
    'Current cash balance maintained by the marking job; set to base_value at inception. With '
    'paper_portfolio_positions this makes market_value recomputable rather than trusted.';

-- 001's text, verbatim.
COMMENT ON COLUMN securities.first_seen IS
    'Earliest known listing date. NULL = listed before our data begins / not yet populated — treat '
    'as unknown-start in point-in-time universe queries, never as never-listed.';
