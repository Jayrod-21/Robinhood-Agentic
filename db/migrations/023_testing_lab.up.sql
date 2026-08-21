-- 023 — the Testing Lab: what was trained, on what data, and how much of it was actually measured.
--
-- SHAPE PORTED from Special-Sprinkle-Sauce `database/migrations/026_training_lab.sql`
-- (experiments / parameter_sweeps / model_snapshots), renamed to the names Joe's work order asks
-- for and tightened in the places that repo's Lab could not tell a real result from a placeholder.
--
-- WHY THE COLUMNS BELOW ARE NOT OPTIONAL
--   The ML library that feeds this table was ported in #117 and #119, and the defect it carried is
--   worth restating here because THIS is where it would become permanent. That library answered
--   "we could not measure this" with the number 0.5 — an untrained model's prediction, a failed
--   ARIMA fit, a model the orchestrator could not run, a missing accuracy in the leaderboard. A
--   fabricated score is bad in memory and worse in a table, because a table is what the leaderboard
--   ranks, what the frontend renders, and what someone reads in six months to decide which model
--   goes near real money.
--
--   So three facts are mandatory on every model_runs row and none of them have a default:
--
--     data_source        — synthetic or real bars. A run on generated data that sorts into the same
--                          leaderboard as a run on real history is the single most expensive lie
--                          this table could tell.
--     metrics_measured   — validation.py returns {"measured": false} when every prediction failed
--                          and it had nothing to score. Those rows must be storable (the failure is
--                          worth keeping) but must never rank.
--     predictions_made / predictions_failed
--                        — a Sharpe computed over 4 surviving predictions out of 200 is not
--                          comparable to one computed over 200, and without both counts the two are
--                          indistinguishable once written down.
--
-- WHAT THIS TABLE MUST NEVER DO
--   Hold production weights, or be read by anything that trades. The Lab proposes; applying a tuned
--   result to live settings is a separate, confirmed, attributed write through
--   PUT /api/settings/{key}, which is already bounded and audited. See the governance note in Joe's
--   work order. Nothing here is granted to anything on the order path.

-- ── experiments: one row per Lab run request ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS experiments (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL,
    kind            text NOT NULL,
    -- No default. Every experiment declares what data backed it, at insert, or it does not insert.
    data_source     text NOT NULL,
    -- The dataset identifier within that source: a seed for synthetic, a symbol + range for bars.
    dataset         text NOT NULL,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    validation_kind text NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    -- Who asked for it. request.state.operator, never an ambient identity — same rule as settings.
    operator        text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    -- Why it ended badly, when it did. A failed experiment that looks like a finished one with no
    -- results is the ambiguity 022 was written to remove, and it applies here too.
    error           text,

    CONSTRAINT ck_experiments_kind CHECK (kind = ANY (ARRAY[
        'train', 'walk_forward', 'sweep', 'stress_test', 'backtest', 'baseline'
    ])),
    CONSTRAINT ck_experiments_data_source CHECK (data_source = ANY (ARRAY[
        'synthetic', 'historical_bars'
    ])),
    CONSTRAINT ck_experiments_validation CHECK (validation_kind = ANY (ARRAY[
        'walk_forward', 'time_series_cv', 'none'
    ])),
    CONSTRAINT ck_experiments_status CHECK (status = ANY (ARRAY[
        'pending', 'running', 'complete', 'failed', 'cancelled'
    ])),
    CONSTRAINT ck_experiments_done CHECK (completed_at IS NULL OR completed_at >= created_at),
    -- An experiment that has not finished has no completion time, and one that has finished does.
    -- Without this a crashed run and a live one are indistinguishable by status alone.
    CONSTRAINT ck_experiments_terminal CHECK (
        (status IN ('pending', 'running') AND completed_at IS NULL)
        OR (status NOT IN ('pending', 'running') AND completed_at IS NOT NULL)
    ),
    -- A failure explains itself. An empty `error` on a failed row is how a real failure becomes
    -- indistinguishable from a run nobody looked at.
    CONSTRAINT ck_experiments_failure_explained CHECK (
        status <> 'failed' OR (error IS NOT NULL AND length(btrim(error)) > 0)
    )
);

CREATE INDEX IF NOT EXISTS ix_experiments_created ON experiments (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_experiments_active  ON experiments (status)
    WHERE status IN ('pending', 'running');

-- ── model_runs: one trained model inside an experiment, and what its metrics are worth ────────

CREATE TABLE IF NOT EXISTS model_runs (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id      bigint NOT NULL REFERENCES experiments (id) ON DELETE CASCADE,
    model_name         text NOT NULL,
    params             jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- The wrapper's get_manifest(), stored verbatim. It carries `trained`, so a row can always be
    -- checked against the model that produced it rather than against what we assume was running.
    manifest           jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics            jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- validation.py's `measured` flag, promoted out of the JSON blob to a column so it can be
    -- constrained and indexed. False means the validator scored nothing — every prediction failed.
    -- Those rows are kept, because a total failure is a result, but they never rank.
    metrics_measured   boolean NOT NULL,
    predictions_made   integer NOT NULL,
    predictions_failed integer NOT NULL,
    -- Where the fitted artifact landed, if it was persisted. NULL is honest: many runs are not.
    artifact_path      text,
    is_baseline        boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_model_runs_counts CHECK (
        predictions_made >= 0
        AND predictions_failed >= 0
        AND predictions_failed <= predictions_made
    ),
    -- The two directions of the same claim. A run that measured nothing cannot have surviving
    -- predictions, and a run where every prediction failed cannot claim to have measured anything.
    -- This is the constraint that stops a fabricated metric from ever being written as a real one.
    CONSTRAINT ck_model_runs_measured_agrees CHECK (
        metrics_measured = (predictions_made > predictions_failed)
    ),
    -- A baseline is the thing everything else is compared against. An unmeasured baseline would
    -- make every comparison in the Lab meaningless.
    CONSTRAINT ck_model_runs_baseline_measured CHECK (NOT is_baseline OR metrics_measured)
);

CREATE INDEX IF NOT EXISTS ix_model_runs_experiment ON model_runs (experiment_id);
-- The leaderboard reads only this index. Unmeasured runs are absent from it by construction, so a
-- ranking query cannot accidentally include one by forgetting a WHERE clause.
CREATE INDEX IF NOT EXISTS ix_model_runs_rankable ON model_runs (model_name, created_at DESC)
    WHERE metrics_measured;
-- One current baseline per model. Without this, "the baseline" silently becomes "whichever baseline
-- row the query happened to return first".
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_runs_baseline ON model_runs (model_name)
    WHERE is_baseline;

-- ── sweeps: one parameter swept across a range, and where it peaked ───────────────────────────

CREATE TABLE IF NOT EXISTS sweeps (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id     bigint NOT NULL REFERENCES experiments (id) ON DELETE CASCADE,
    param             text NOT NULL,
    metric            text NOT NULL,
    -- [{"value": 0.3, "metric": 1.21, "measured": true}, ...] — one entry per point tested,
    -- carrying its own measured flag so a sweep with holes in it reads as a sweep with holes.
    points            jsonb NOT NULL DEFAULT '[]'::jsonb,
    best_value        numeric,
    best_metric_value numeric,
    -- How many points were tested and how many produced a real number. A sweep that "peaked" at the
    -- only value it managed to measure is not a peak, and these two columns are what make that
    -- visible without parsing the blob.
    points_tested     integer NOT NULL,
    points_measured   integer NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sweeps_counts CHECK (
        points_tested >= 0 AND points_measured >= 0 AND points_measured <= points_tested
    ),
    -- A best value must have come from somewhere. Reporting a winner out of zero measured points
    -- is the sweep-shaped version of returning 0.5.
    CONSTRAINT ck_sweeps_best_was_measured CHECK (
        (best_value IS NULL AND best_metric_value IS NULL) OR points_measured > 0
    )
);

CREATE INDEX IF NOT EXISTS ix_sweeps_experiment ON sweeps (experiment_id);

GRANT SELECT, INSERT, UPDATE ON experiments, model_runs, sweeps TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE experiments_id_seq, model_runs_id_seq, sweeps_id_seq TO rh_app;
