-- 019 — operator-tunable parameters, with the history of who changed what.
--
-- WHY THESE LEAVE THE SOURCE
--   The guardrail thresholds lived as module constants: DRIFT_TOLERANCE_PCT, MAX_POSITION_PCT, the
--   cash band, the off-factor floor. Changing one meant editing Python and redeploying, which in
--   practice means they never changed — and a guardrail nobody can tune is one that eventually gets
--   ignored instead of adjusted. The standing instruction is that guardrails must be tunable,
--   observable, and overridable, never a silent block.
--
-- WHY A HISTORY TABLE AND NOT JUST A VALUE
--   A threshold is a claim about how the book should behave, and a breach reported under it is only
--   auditable if you can say what the threshold WAS at the time. Overwriting in place would make
--   "why did this not fire last week?" unanswerable. So every change appends, and the current value
--   is a row that names its last writer.
--
-- WHAT IS DELIBERATELY NOT HERE
--   The hard stop and the trim multiple. Those are parsed out of docs/SLATE.md on purpose: the
--   document an owner edits is meant to win, and moving them here would invert that — the dashboard
--   would quietly outrank the written plan. They are shown read-only in the UI, pointing at the file.
--
-- NUMERIC, NOT FLOAT
--   These are compared against percentages and money-derived ratios. numeric keeps 1.5 as 1.5.

CREATE TABLE IF NOT EXISTS app_settings (
    key         text PRIMARY KEY,
    value       numeric NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    -- The operator email, not a user id: this table outlives any particular auth schema, and the
    -- point of the column is that a human can read who moved a guardrail without a join.
    updated_by  text,
    CONSTRAINT ck_app_settings_key CHECK (key ~ '^[a-z][a-z0-9_]{2,63}$')
);

CREATE TABLE IF NOT EXISTS app_settings_history (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key         text NOT NULL,
    old_value   numeric,          -- NULL on the first write for a key
    new_value   numeric NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now(),
    changed_by  text,
    CONSTRAINT ck_app_settings_history_change CHECK (old_value IS DISTINCT FROM new_value)
);

CREATE INDEX IF NOT EXISTS ix_app_settings_history_key
    ON app_settings_history (key, changed_at DESC);

-- rh_app may read and write the current values, and may APPEND history — but never rewrite it.
-- Same shape as the knowledge base: the record of what was believed is not editable by the
-- application that produced it.
GRANT SELECT, INSERT, UPDATE, DELETE ON app_settings         TO rh_app;
GRANT SELECT, INSERT                 ON app_settings_history TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE app_settings_history_id_seq  TO rh_app;
