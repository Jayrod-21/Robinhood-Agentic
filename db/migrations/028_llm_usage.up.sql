-- 028 — who paid for which model call.
--
-- WHY
--   Two owners share this system and the bill was landing entirely on one of them: measured
--   2026-08-26, roughly $9 of Anthropic spend on Jared's key and nothing on Joe's. Nothing recorded
--   per-key spend, so there was no way to split it and no way to check whether a split was working.
--
-- WHY ROUND-ROBIN WOULD NOT HAVE FIXED IT
--   Alternating keys 50/50 from today leaves that $9 gap in place forever — it freezes the
--   imbalance rather than closing it. Converging requires preferring whoever is BEHIND, which
--   requires knowing what each has spent. That is this table's whole job.
--
-- TOKENS ARE A FACT, DOLLARS ARE AN ASSUMPTION
--   The provider reports tokens and they never change. A dollar figure depends on a price list this
--   project reads from documentation rather than from a billing API, so it can drift without
--   anything noticing. Both are stored, and `pricing_version` records which price list produced the
--   dollars — so a rate change or a wrong rate can be found and repriced instead of leaving a ledger
--   that is quietly incorrect in a way nobody can locate.
--
--   Same shape as intraday_observations' formula_version (#133), and it matters more here, because
--   this ledger decides who owes whom.
--
-- IT STARTS AT ZERO, DELIBERATELY
--   An earlier draft seeded the ~$9 already spent so balancing would begin from the real position.
--   The owners decided that history is not worth settling: the split applies GOING FORWARD only.
--   That is recorded here because the table would otherwise look like it was designed by someone
--   who forgot about the prior spend, rather than by someone who was told to ignore it.
--
--   The policy is unchanged and still not round-robin. Even from a level start, calls cost
--   different amounts — a Haiku juror and a Sonnet synthesis are not the same money — so
--   alternating by CALL COUNT would not split by DOLLARS. Selection tracks dollars.
--
-- estimated_cost_usd IS NULLABLE ON PURPOSE
--   NULL means "this model has no published rate in the pricing table" — a new model dropped into
--   JURY_MODEL, for instance. Zero would be a claim that the call was free, and would under-report
--   exactly the spend nobody is watching.

CREATE TABLE IF NOT EXISTS llm_usage (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at        timestamptz NOT NULL DEFAULT now(),
    provider           text NOT NULL,
    -- The OWNER label, from ANTHROPIC_API_KEY_NAME / GEMINI_API_KEY_NAME_2 and friends — never a
    -- slot number. The environment does not follow "_1 is always the same person": Anthropic slot 1
    -- is Jared and Gemini slot 1 is Joe. Attributing by position would misattribute every Gemini
    -- call, and a cost ledger that misattributes is worse than none — it is confidently wrong about
    -- who owes whom.
    key_owner          text NOT NULL,
    model              text NOT NULL,
    -- What the work was, so a bill can be explained rather than just totalled.
    purpose            text,
    calls              integer NOT NULL DEFAULT 1,
    input_tokens       bigint NOT NULL DEFAULT 0,
    output_tokens      bigint NOT NULL DEFAULT 0,
    cache_read_tokens  bigint NOT NULL DEFAULT 0,
    estimated_cost_usd numeric(14, 6),
    pricing_version    integer NOT NULL,

    CONSTRAINT ck_llm_usage_provider CHECK (provider = ANY (ARRAY['anthropic', 'gemini'])),
    CONSTRAINT ck_llm_usage_owner CHECK (length(btrim(key_owner)) > 0),
    CONSTRAINT ck_llm_usage_counts CHECK (
        calls >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0
    ),
    CONSTRAINT ck_llm_usage_cost CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    CONSTRAINT ck_llm_usage_pricing CHECK (pricing_version >= 1)
);

-- The question this table exists to answer: what has each owner spent.
CREATE INDEX IF NOT EXISTS ix_llm_usage_owner ON llm_usage (provider, key_owner, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_usage_time ON llm_usage (occurred_at DESC);
-- Finding every row priced under a superseded price list, so it can be repriced.
CREATE INDEX IF NOT EXISTS ix_llm_usage_pricing ON llm_usage (pricing_version);

COMMENT ON TABLE llm_usage IS
    'Per-key LLM spend, so two owners can split one bill. key_owner comes from the *_NAME env '
    'labels, never from slot position — Anthropic slot 1 is Jared but Gemini slot 1 is Joe. '
    'Tokens are recorded as fact; estimated_cost_usd is derived from backend/app/llm/pricing.py at '
    'pricing_version, and NULL means the model had no published rate. See issue #133 for the same '
    'fact-vs-assumption split applied to ratios.';

-- A running total per owner. The selection path reads this on every call, so it is a view rather
-- than a query duplicated at each call site.
CREATE OR REPLACE VIEW llm_spend_by_owner AS
    SELECT provider,
           key_owner,
           sum(calls)                       AS calls,
           sum(input_tokens)                AS input_tokens,
           sum(output_tokens)               AS output_tokens,
           -- Rows with no published rate contribute 0 to the total but are counted separately, so
           -- an owner's total is never silently understated without the understatement being
           -- visible right beside it.
           coalesce(sum(estimated_cost_usd), 0)              AS estimated_cost_usd,
           count(*) FILTER (WHERE estimated_cost_usd IS NULL) AS unpriced_rows
      FROM llm_usage
     GROUP BY provider, key_owner;

GRANT SELECT, INSERT ON llm_usage TO rh_app;
GRANT SELECT ON llm_spend_by_owner TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE llm_usage_id_seq TO rh_app;
