-- Destructive by FILENAME: dropping a column destroys its data, and the runner checks the name.
--
-- Fourth time this argument has come up (014, 022, 023). The tempting version here is that these
-- columns are "just a cached view of what /api/reconciliation would say anyway". They are not: that
-- endpoint answers about the book RIGHT NOW. What is dropped is the record of what the book looked
-- like at the moment a cycle decided which positions to debate — which cannot be recomputed from a
-- broker that has moved on.
ALTER TABLE cycle_runs
    DROP CONSTRAINT IF EXISTS ck_cycle_runs_recon_agrees,
    DROP CONSTRAINT IF EXISTS ck_cycle_runs_recon_counts,
    DROP CONSTRAINT IF EXISTS ck_cycle_runs_recon_ran;

DROP INDEX IF EXISTS ix_cycle_runs_desync;

ALTER TABLE cycle_runs
    DROP COLUMN IF EXISTS recon_findings,
    DROP COLUMN IF EXISTS recon_breaches,
    DROP COLUMN IF EXISTS recon_unexpected,
    DROP COLUMN IF EXISTS recon_missing,
    DROP COLUMN IF EXISTS recon_drifted,
    DROP COLUMN IF EXISTS recon_matched,
    DROP COLUMN IF EXISTS in_sync,
    DROP COLUMN IF EXISTS reconciled_at;
