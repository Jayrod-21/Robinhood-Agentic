-- 017 — one row per OBSERVATION, not per run.
--
-- 003 made fundamentals_snapshots unique on
--     (security_id, period_end, period_type, source_id, known_at)
-- with the reasoning that a restatement is a new fact rather than a correction applied in place.
-- The reasoning is right; the key is wrong. source_id changes on every RUN, so re-running the
-- loader appended a duplicate of the same observation — NVDA accumulated three identical rows for
-- FY2026 within an hour, and any history view would have shown the same period three times.
--
-- A restatement does not arrive with a new source id. It arrives with a new ACCEPTANCE DATE, which
-- is already in the key as known_at. So the observation is identified by
--     (security_id, period_end, period_type, known_at)
-- and source_id becomes what it should always have been: provenance, not identity.
--
-- Re-running now UPDATES the row in place, which is correct for exactly the case that motivated
-- this migration: the ingest learned to map twenty more fields, and the older rows should gain them
-- rather than sit beside newer ones as half-empty twins.

-- Collapse the existing duplicates first, keeping the most recently written of each set: it was
-- produced by the newest mapping and therefore carries the most fields.
DELETE FROM fundamentals_snapshots f
WHERE EXISTS (
    SELECT 1 FROM fundamentals_snapshots g
    WHERE g.security_id = f.security_id
      AND g.period_end  = f.period_end
      AND g.period_type = f.period_type
      AND g.known_at IS NOT DISTINCT FROM f.known_at
      AND g.id > f.id
);

ALTER TABLE fundamentals_snapshots DROP CONSTRAINT IF EXISTS uq_fundamentals_snapshot;

ALTER TABLE fundamentals_snapshots
    ADD CONSTRAINT uq_fundamentals_observation
    UNIQUE NULLS NOT DISTINCT (security_id, period_end, period_type, known_at);

COMMENT ON CONSTRAINT uq_fundamentals_observation ON fundamentals_snapshots IS
    'One row per observation. known_at (the filing acceptance date) distinguishes a restatement '
    'from a re-fetch; source_id does NOT, which is why it is provenance rather than identity.';
