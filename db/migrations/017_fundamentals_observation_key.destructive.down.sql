-- Reverse of 017. Destructive in effect: restoring the old key cannot recover the duplicate rows
-- 017 deleted, and re-running the loader under it will start appending per-run duplicates again.
--
-- Recreates an INDEX, not a table constraint. 003 created uq_fundamentals_snapshot with CREATE
-- UNIQUE INDEX, so 017's first draft "dropped" it with ALTER TABLE ... DROP CONSTRAINT IF EXISTS,
-- which matched nothing and silently left it in place — and this down then failed trying to add a
-- name that still existed. IF EXISTS on the wrong object type is a no-op that reads like a success.
ALTER TABLE fundamentals_snapshots DROP CONSTRAINT IF EXISTS uq_fundamentals_observation;
DROP INDEX IF EXISTS uq_fundamentals_observation;
CREATE UNIQUE INDEX IF NOT EXISTS uq_fundamentals_snapshot
    ON fundamentals_snapshots (security_id, period_end, period_type, source_id, known_at)
    NULLS NOT DISTINCT;
