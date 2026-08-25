-- 024 — did the cycle check its own premises, and what did it find?
--
-- WHAT WENT WRONG THAT THIS RECORDS
--   /api/reconciliation has been able to answer "does the broker hold what the slate says" since
--   issue #22's first half shipped. Nothing ever asked it. The twice-daily cycle read docs/SLATE.md,
--   debated positions against it, and wrote a report — for weeks after the account of record moved
--   from Robinhood to Alpaca, while the live book matched the document on ZERO of eighteen names:
--   3 documented positions absent, 5 drifted to a twentieth of their targets, 10 held and
--   undocumented, cash at 92.5% against a 10-20% band. A page nobody opens is not a control.
--
-- WHY ON cycle_runs AND NOT ITS OWN TABLE
--   The reconciliation is a fact ABOUT a cycle run: it is what that run believed when it decided
--   what to debate. Split into its own table it would need a join to answer the only question worth
--   asking of it — "was the cycle that produced this report reasoning about a real portfolio?" —
--   and a run with no matching row would be ambiguous between "in sync" and "never checked".
--
-- WHY reconciled_at IS SEPARATE FROM in_sync
--   NULL in_sync means the check did not run (no snapshot, no slate, a broker error). FALSE means
--   it ran and the book does not match. Collapsing those into one boolean is how "we never looked"
--   comes to render identically to "we looked and it was fine" — the exact failure this migration
--   exists because of.

ALTER TABLE cycle_runs
    ADD COLUMN IF NOT EXISTS reconciled_at      timestamptz,
    ADD COLUMN IF NOT EXISTS in_sync            boolean,
    ADD COLUMN IF NOT EXISTS recon_matched      integer,
    ADD COLUMN IF NOT EXISTS recon_drifted      integer,
    ADD COLUMN IF NOT EXISTS recon_missing      integer,
    ADD COLUMN IF NOT EXISTS recon_unexpected   integer,
    ADD COLUMN IF NOT EXISTS recon_breaches     integer,
    -- The findings themselves, so a report can be reconstructed without re-querying a broker whose
    -- answer will have changed by then. Positions and failing checks only — a matched name carries
    -- no information worth the row width.
    ADD COLUMN IF NOT EXISTS recon_findings     jsonb;

-- The counts only exist when the check ran, and are non-negative when it did. Without this, a
-- partially-written row could report "0 missing" for a check that never looked for any.
ALTER TABLE cycle_runs
    ADD CONSTRAINT ck_cycle_runs_recon_ran CHECK (
        (reconciled_at IS NULL AND in_sync IS NULL AND recon_matched IS NULL)
        OR (reconciled_at IS NOT NULL AND in_sync IS NOT NULL AND recon_matched IS NOT NULL)
    ),
    ADD CONSTRAINT ck_cycle_runs_recon_counts CHECK (
        (recon_matched    IS NULL OR recon_matched    >= 0)
        AND (recon_drifted    IS NULL OR recon_drifted    >= 0)
        AND (recon_missing    IS NULL OR recon_missing    >= 0)
        AND (recon_unexpected IS NULL OR recon_unexpected >= 0)
        AND (recon_breaches   IS NULL OR recon_breaches   >= 0)
    ),
    -- in_sync and the counts must tell the same story. A run claiming to be in sync while holding
    -- an undocumented position is the written-claim-that-means-something-else defect in a column.
    ADD CONSTRAINT ck_cycle_runs_recon_agrees CHECK (
        in_sync IS NULL
        OR in_sync = (
            COALESCE(recon_drifted, 0) = 0
            AND COALESCE(recon_missing, 0) = 0
            AND COALESCE(recon_unexpected, 0) = 0
            AND COALESCE(recon_breaches, 0) = 0
        )
    );

-- The question an operator actually asks: when did this last go wrong, and is it wrong now.
CREATE INDEX IF NOT EXISTS ix_cycle_runs_desync ON cycle_runs (started_at DESC)
    WHERE in_sync IS FALSE;
