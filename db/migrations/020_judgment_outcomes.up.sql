-- 020 — what actually happened after each call, so calibration has something to grade.
--
-- WHY THIS TABLE DID NOT EXIST
--   The schema modelled debates, proposals and judgments — the CLAIMS — but nothing recorded how
--   they turned out. /api/calibration has been returning zeros with the note "outcome scoring is
--   not wired yet", which was accurate: there was nowhere to write an outcome.
--
-- ONE ROW PER JUDGMENT, AND IT IS DERIVED
--   Unlike a judgment (a belief someone held, which is never edited) an outcome is a computation
--   over prices. If the rule changes, the right thing is to recompute, not to keep both answers.
--   So the judgment is the primary key and a re-score replaces in place — with `scoring_basis`
--   naming the rule that produced the row, so a change of rule is visible rather than a silent
--   shift in everyone's track record.
--
-- WHAT "CORRECT" MEANS, WRITTEN DOWN
--   /api/calibration already declares it: a positive counterfactual return over 5 trading days.
--   Applied per decision: buy and hold are correct when the name rose, sell when it fell. An
--   `escalate` is not a directional call and gets no row at all — grading it either way would
--   invent an opinion the juror explicitly declined to give.
--
-- UNRESOLVED IS ABSENT, NOT FALSE
--   A judgment whose horizon has not elapsed simply has no row here. The contract is explicit that
--   such calls are EXCLUDED from the bins rather than counted as misses: scoring an unknown as a
--   failure would make every recent call look like a bad one.

CREATE TABLE IF NOT EXISTS judgment_outcomes (
    judgment_id     bigint PRIMARY KEY REFERENCES judgments(id) ON DELETE CASCADE,
    horizon_days    integer NOT NULL,
    decision_date   date NOT NULL,
    outcome_date    date NOT NULL,
    entry_price     numeric(18,6) NOT NULL,
    exit_price      numeric(18,6) NOT NULL,
    forward_return  numeric(12,8) NOT NULL,
    is_correct      boolean NOT NULL,
    scoring_basis   text NOT NULL,
    scored_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_judgment_outcomes_horizon CHECK (horizon_days >= 1),
    -- The outcome must come after the decision. A window that runs backwards would grade a call on
    -- information from before it was made.
    CONSTRAINT ck_judgment_outcomes_window CHECK (outcome_date > decision_date),
    CONSTRAINT ck_judgment_outcomes_prices CHECK (entry_price > 0 AND exit_price > 0)
);

CREATE INDEX IF NOT EXISTS ix_judgment_outcomes_date
    ON judgment_outcomes (outcome_date DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON judgment_outcomes TO rh_app;
