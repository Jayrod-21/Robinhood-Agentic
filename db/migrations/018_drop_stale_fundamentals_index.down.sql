-- Recreates the redundant index. Not destructive: it drops no data and enforces nothing new.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fundamentals_snapshot
    ON fundamentals_snapshots (security_id, period_end, period_type, source_id, known_at)
    NULLS NOT DISTINCT;
