-- 022 — what the twice-daily cycle is doing right now.
--
-- WHY THE DATABASE AND NOT MEMORY
--   bin/scheduled_cycle.sh runs `docker compose exec backend python -m app.jobs.cycle`, which is a
--   DIFFERENT PROCESS from the uvicorn workers serving the pages. So an in-memory publisher — the
--   obvious way to make a stream joinable — cannot reach the API at all. The database is the only
--   channel that crosses that boundary, and it has the side benefit of surviving a restart and
--   answering "what was slow last Tuesday" for free.
--
-- WHAT IT REPLACES
--   Nothing. A cycle ran for twenty minutes, debated fifteen positions, and the only artifact was
--   a markdown report written at the end. There was no way to tell a cycle in progress from one
--   that had crashed, or from one that was never scheduled — the same class of invisibility as the
--   backup that had not run in three weeks.
--
-- PER-DEBATE GRANULARITY, DELIBERATELY
--   `completed_positions` and `current_symbol`, not per-juror progress. A debate takes about
--   eighty seconds; tracking eleven jurors inside it would mean ~330 extra writes a day to render
--   a line that says "6 of 10 jurors voted". Per-debate carries most of the value at a fraction of
--   the write amplification, and finer granularity can be added without changing this shape.

CREATE TABLE IF NOT EXISTS cycle_runs (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    phase               text NOT NULL,
    status              text NOT NULL DEFAULT 'running',
    started_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    total_positions     integer,
    completed_positions integer NOT NULL DEFAULT 0,
    current_symbol      text,
    scanned             integer,
    survivors           integer,
    -- Why it ended badly, when it did. A failed cycle that looks identical to a finished one is
    -- the thing this table exists to prevent.
    error               text,

    CONSTRAINT ck_cycle_runs_phase  CHECK (phase = ANY (ARRAY['open','close'])),
    CONSTRAINT ck_cycle_runs_status CHECK (status = ANY (ARRAY['running','complete','failed'])),
    CONSTRAINT ck_cycle_runs_done   CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT ck_cycle_runs_counts CHECK (
        completed_positions >= 0
        AND (total_positions IS NULL OR completed_positions <= total_positions)
    ),
    -- A run that is still running has not finished, and one that has finished is not running.
    -- Without this, a crashed job and a live one are indistinguishable by status alone.
    CONSTRAINT ck_cycle_runs_terminal CHECK (
        (status = 'running' AND completed_at IS NULL)
        OR (status <> 'running' AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_cycle_runs_started ON cycle_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_cycle_runs_active ON cycle_runs (status) WHERE status = 'running';

GRANT SELECT, INSERT, UPDATE ON cycle_runs TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE cycle_runs_id_seq TO rh_app;
