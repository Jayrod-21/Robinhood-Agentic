-- 008_gap_audit — a review queue for internal price-series holes, replacing the splice
-- threshold-guess with gap-plus-evidence classification.
--
-- Issued by the Phase A round-2 fix-pass (docs/fixpass/REVIEW_FIXES_phaseA.md, B-N2).
--
-- WHY (B-N2 — recycled tickers below any threshold)
--   The B-S5 splice ran at a gap-length threshold: first 180 days (the review's definition), then
--   120 (after META's 132-day recycle was caught). The re-review then confirmed 5 of 5 sampled
--   60-120-day extremes as real identity breaks (COHR, DBD, VRM, FNGU, FIG) — and this pass's own
--   audit found C3.ai's December-2020 IPO recycling Arlington Asset's "AI" ticker across a 47-DAY
--   gap. A bare length threshold is the wrong mechanism: every move finds more, and the next
--   recycle is always just underneath. Length alone cannot distinguish a recycled ticker from a
--   genuinely halted issuer; corroborating evidence can.
--
--   So the mechanism becomes: enumerate every internal hole above a small floor, classify each
--   with EVIDENCE (the split-adjusted cross-gap price ratio, then the provider's own history for
--   the symbol), store the classification HERE, and require a disposition — a hole that has not
--   been classified fails verification loudly (db/verify_daily_series.py check 7) instead of
--   sitting silently in the return series. What the evidence cannot see is COUNTED in this table
--   (disposition 'halt_consistent'), never assumed empty.
--
--   The floor is 10 missed covered sessions — the one number here that is NOT a guess: SEC Rule
--   12(k) caps a trading suspension at 10 business days, and exchange news/volatility halts are
--   far shorter, so a security absent for MORE than 10 sessions the rest of the archive traded is
--   outside every routine-halt mechanism and must be classified. Below the floor, out-of-band
--   moves are overwhelmingly real (post-halt crashes, squeezes); the audit pass counts and logs
--   that cohort (measured 2026-07-29: 329 sub-floor holes with an out-of-band ratio across the
--   1-9-session bands) rather than pretending it is empty.
--
-- migrate: filename carries no destructive marker — this migration only creates.

CREATE TABLE IF NOT EXISTS price_gap_audit (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id      BIGINT      NOT NULL REFERENCES securities (id),
    -- Denormalised deliberately: after a splice the hole's bars belong to two identities and the
    -- pre-gap row keeps this audit entry; the symbol string is what an operator greps for.
    symbol           TEXT        NOT NULL,
    gap_start        DATE        NOT NULL,   -- last bar BEFORE the hole
    gap_resume       DATE        NOT NULL,   -- first bar AFTER the hole
    gap_days         INTEGER     NOT NULL,   -- calendar days, gap_resume - gap_start
    -- Trading sessions inside the hole where the REST of the archive has bars. This is the honest
    -- unit: the universe-wide December-2024 hole contributes zero, so it cannot masquerade as a
    -- per-security absence.
    missed_sessions  INTEGER     NOT NULL,
    close_before     NUMERIC(18, 6) NOT NULL,
    close_after      NUMERIC(18, 6) NOT NULL,
    -- (close_after / close_before) × Π(recorded split ratios with ex_date inside the hole): the
    -- cross-gap move NET of every action we know about. A discontinuity this column cannot
    -- explain is the evidence that two issuers may be spliced.
    adj_ratio        NUMERIC(24, 8) NOT NULL,
    disposition      TEXT        NOT NULL,
    evidence         TEXT,                    -- free text: what the classifier / provider / operator saw
    source_id        BIGINT      REFERENCES data_sources (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_price_gap_audit_hole UNIQUE (security_id, gap_start),
    CONSTRAINT ck_price_gap_audit_order CHECK (gap_resume > gap_start),
    CONSTRAINT ck_price_gap_audit_disposition CHECK (disposition IN (
        -- non-terminal (verification FAILS while any of these remain):
        'pending_review',        -- out-of-band ratio, no provider evidence yet
        'identity_break',        -- provider history begins at/after gap_resume: two issuers — splice it
        'provider_unresolvable', -- provider cannot speak for this symbol; splice is the conservative default
        'split_missing',         -- provider's own cross-gap ratio is in-band while ours is not:
                                 -- an action inside the hole is unrecorded — fetch it, adjust, re-audit
        -- terminal (verification passes):
        'halt_consistent',       -- ratio inside the band: consistent with one issuer suspended.
                                 -- COUNTED here precisely because ratio evidence cannot see a
                                 -- similar-priced recycle — this is the tool's stated blind spot,
                                 -- stored, not assumed empty.
        'continuity_confirmed',  -- provider history spans the pre-gap dates: one issuer, real move
        'spliced',               -- split into two identities by load_delistings.py splice
        'halt_accepted'          -- operator override: reviewed and accepted as one issuer (record
                                 -- WHO and WHY in evidence — this is the tunable/overridable path)
    ))
);

DROP TRIGGER IF EXISTS trg_price_gap_audit_updated_at ON price_gap_audit;
CREATE TRIGGER trg_price_gap_audit_updated_at
    BEFORE UPDATE ON price_gap_audit
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS ix_price_gap_audit_disposition ON price_gap_audit (disposition);

COMMENT ON TABLE price_gap_audit IS
    'Review queue for internal holes in per-security daily series (>= 10 missed covered '
    'sessions). Written by load_delistings.py audit; consumed by splice --from-audit and by '
    'verify_daily_series.py check 7, which FAILS while any non-terminal disposition remains — a '
    'possible two-issuer splice must be classified, never silently tolerated. halt_consistent '
    'rows are the counted blind spot: an in-band ratio cannot distinguish a suspension from a '
    'similar-priced recycle.';
COMMENT ON COLUMN price_gap_audit.missed_sessions IS
    'Trading sessions inside the hole where the rest of the archive HAS bars — universe-wide '
    'coverage gaps (e.g. the 15-session December-2024 hole) are excluded, so this measures the '
    'security''s own absence, not the archive''s.';
COMMENT ON COLUMN price_gap_audit.adj_ratio IS
    '(close_after / close_before) × Π(recorded split ratios inside the hole). Outside '
    '[0.5, 2.0] = a discontinuity no recorded action explains (band justified by measurement, '
    'see load_delistings.py AUDIT_RATIO_*). In-band does NOT prove continuity — see disposition '
    'halt_consistent.';
COMMENT ON COLUMN price_gap_audit.disposition IS
    'Classification state. Non-terminal (pending_review / identity_break / provider_unresolvable '
    '/ split_missing) fails verify_daily_series check 7. Terminal: halt_consistent, '
    'continuity_confirmed, spliced, halt_accepted. halt_accepted is the operator override — set '
    'it by hand with evidence text saying who decided and why.';

-- ── corrections to prose comments in APPLIED migrations ──────────────────────────────────────
-- 004/005/006/007 are applied and checksummed; their `--` header comments cannot be edited
-- (ChecksumMismatch by design) and are not catalog comments, so no COMMENT ON can replace them.
-- The corrections therefore live here, at the earliest point in migration order a reader can
-- encounter them (untrue-claim audit, docs/fixpass/REVIEW_FIXES_phaseA.md):
--
--   * 004_evaluation.up.sql:484 and 005_corporate_actions.up.sql:18 state the marking formula
--     as `Σ shares × adj_close + cash`. That formula mixes share bases and is SUPERSEDED: marking
--     is `Σ shares × RAW close + cash` with lot share counts kept on the as-traded basis. The
--     authoritative statement is 007's COMMENT ON paper_portfolio_positions /
--     paper_portfolios.cash — the catalog, not the old headers, is what a marking-job author
--     must read. 005's dividend argument (credit cash, never dividend-adjust the price) survives
--     the corrected premise unchanged.
--   * 006_split_factor.up.sql:8 says NUWE's cumulative factor is "≈ 1e-10"; the measured full
--     product of its 8 recorded splits is 7.71e-13 (which is also the only figure consistent
--     with 006's own "$21 trillion" implied level). 006:38's claim that NUMERIC(30,12) covers
--     "every ratio observed and a wide margin" is likewise wrong for UNBOUNDED products
--     (7.71e-13 is below the column's 1e-12 granularity and would round to 0, violating
--     ck_price_bars_daily_factor); it is safe in practice only because 007's as-of bound keeps
--     every STORED factor well above that — a real dependency of 006's column on 007's bound.
--   * 007_point_in_time.up.sql:16 says 308,709 bars carried post-decision information. That was
--     measured before the B-S5 splice re-attributed actions; the figure on the repaired database
--     is 304,870 bars across 436 securities (measured 2026-07-29). The defect and the fix are
--     unchanged; the count drifted.
