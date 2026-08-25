-- Destructive by FILENAME. It drops constraints and indexes, and — the part that actually destroys
-- data — it must reset any row carrying the new disposition, because the old CHECK does not permit
-- it and the table would refuse to accept the constraint back.
--
-- Fifth outing for "it is only derived, we can recompute it" (014, 022, 023, 024). Weaker than
-- usual here, and still wrong: security_type is recomputable from two FMP calls, but the
-- non_common_instrument dispositions carry the JUDGEMENT that a hole was explained, and resetting
-- them to provider_unresolvable loses which holes a human has already looked at. That is the same
-- distinction 024 draws between "we never looked" and "we looked and it was fine".
UPDATE price_gap_audit SET disposition = 'provider_unresolvable'
 WHERE disposition = 'non_common_instrument';

ALTER TABLE price_gap_audit
    DROP CONSTRAINT IF EXISTS ck_price_gap_audit_disposition;

ALTER TABLE price_gap_audit
    ADD CONSTRAINT ck_price_gap_audit_disposition CHECK (disposition = ANY (ARRAY[
        'pending_review', 'identity_break', 'provider_unresolvable', 'split_missing',
        'halt_consistent', 'continuity_confirmed', 'spliced', 'halt_accepted'
    ]));

DROP INDEX IF EXISTS ix_securities_type;
DROP INDEX IF EXISTS ix_securities_investable;

COMMENT ON COLUMN securities.security_type IS NULL;

ALTER TABLE securities DROP CONSTRAINT IF EXISTS ck_securities_security_type;
