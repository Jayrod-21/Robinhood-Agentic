-- 014_orders — the execution audit trail (docs/EXECUTION_DESIGN.md).
--
-- Written BEFORE any execution code exists, deliberately: the audit table is what every other part
-- of the path writes into, and a schema retrofitted around working code records what was convenient
-- rather than what is needed.
--
-- WHAT THIS TABLE IS FOR
--     Answering, months later: who placed this, what did they approve, what did the guardrails say
--     at the time, and did the fill match the intent. Every one of those is a different column,
--     because merging intent with outcome is how a ledger drifts from an account while still
--     looking self-consistent.
--
-- INTENT AND OUTCOME ARE SEPARATE, ALWAYS
--     `requested_*` is what the operator approved. `filled_*` is what the broker did. An order can
--     be accepted, partially filled, cancelled or rejected minutes later; recording intent as though
--     it were outcome puts this table permanently out of step with the account it claims to describe.
--     A row is INSERTed at submission and UPDATEd on reconciliation — never rewritten in place to
--     make the two agree.

CREATE TABLE orders (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Idempotency. Derived from the preview, sent to the broker as client_order_id, and UNIQUE here
    -- so a retry after an ambiguous timeout collides locally even if the request never reached
    -- Alpaca. This is the column that stops "did that go through?" from becoming two positions.
    client_order_id   text        NOT NULL UNIQUE,
    CONSTRAINT ck_orders_client_id_shape CHECK (client_order_id ~ '^[A-Za-z0-9._:-]{8,128}$'),

    -- The preview the operator actually approved, kept verbatim. Not a foreign key: previews are
    -- short-lived and this must outlive them. What was on screen at the moment of approval is the
    -- record, and regenerating it later from current prices would answer a different question.
    preview_id        text        NOT NULL,
    preview           jsonb       NOT NULL,
    CONSTRAINT ck_orders_preview_obj CHECK (jsonb_typeof(preview) = 'object'),

    -- WHO. Both operators may execute (design §6.1); nullable only so a delete never erases the
    -- order, matching auth_events' ON DELETE SET NULL reasoning — an audit row outlives its actor.
    operator_id       bigint      REFERENCES operators(id) ON DELETE SET NULL,

    -- WHICH ACCOUNT. Not inferred from config at read time: config changes, and a row must say what
    -- was true when it was written. 'alpaca-paper' | 'alpaca-live'.
    broker_env        text        NOT NULL,
    CONSTRAINT ck_orders_broker_env CHECK (broker_env IN ('alpaca-paper', 'alpaca-live')),
    account_masked    text        NOT NULL,

    -- INTENT — what was approved.
    symbol            text        NOT NULL,
    CONSTRAINT ck_orders_symbol CHECK (symbol ~ '^[A-Z]{1,5}(\.[A-Z])?$'),
    side              text        NOT NULL,
    CONSTRAINT ck_orders_side CHECK (side IN ('buy', 'sell')),
    order_type        text        NOT NULL,
    CONSTRAINT ck_orders_type CHECK (order_type IN ('limit', 'market')),
    time_in_force     text        NOT NULL,
    -- Shares, always. The preview shows both units; exactly one reaches the broker, because
    -- shares-versus-notional is the units confusion that makes a $25 position a $25,000 one.
    requested_qty     numeric(20, 8) NOT NULL,
    CONSTRAINT ck_orders_qty_positive CHECK (requested_qty > 0),
    -- NULL for market orders; required for limit, enforced below rather than by convention.
    limit_price       numeric(18, 6),
    CONSTRAINT ck_orders_limit_price CHECK (
        (order_type = 'limit' AND limit_price IS NOT NULL AND limit_price > 0)
        OR (order_type = 'market' AND limit_price IS NULL)
    ),

    -- GUARDRAILS as evaluated at approval. Detail lives in guardrail_events; this is the verdict, so
    -- "was anything overridden on this order" is answerable without a join.
    guardrails_passed boolean     NOT NULL,
    override_by       bigint      REFERENCES operators(id) ON DELETE SET NULL,
    override_reason   text,
    -- An override without a stated reason is a flag flip pretending to be a decision.
    CONSTRAINT ck_orders_override_paired CHECK (
        (override_by IS NULL AND override_reason IS NULL)
        OR (override_by IS NOT NULL AND override_reason IS NOT NULL AND length(trim(override_reason)) >= 8)
    ),
    -- Guardrails failing without an override means the order should never have been submitted.
    CONSTRAINT ck_orders_failed_needs_override CHECK (guardrails_passed OR override_by IS NOT NULL),

    -- SUBMISSION. `submitted_at` is stamped BEFORE the broker call so an order that vanishes between
    -- request and response still leaves evidence it was attempted; broker_order_id arrives after.
    submitted_at      timestamptz NOT NULL DEFAULT now(),
    broker_order_id   text,
    submit_status     text        NOT NULL DEFAULT 'submitting',
    CONSTRAINT ck_orders_submit_status CHECK (
        submit_status IN ('submitting', 'accepted', 'rejected', 'unknown')
    ),
    submit_error      text,

    -- OUTCOME, reconciled from the broker afterwards. Deliberately all nullable: an unreconciled
    -- order is a normal state, and defaulting these to zero would assert a fill that did not happen.
    broker_status     text,
    filled_qty        numeric(20, 8),
    filled_avg_price  numeric(18, 6),
    reconciled_at     timestamptz,
    CONSTRAINT ck_orders_filled_qty CHECK (filled_qty IS NULL OR filled_qty >= 0),

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE orders IS
    'Execution audit trail. One row per submission attempt. requested_* is what the operator '
    'approved; filled_* is what the broker did; they are never merged. See docs/EXECUTION_DESIGN.md.';
COMMENT ON COLUMN orders.client_order_id IS
    'Idempotency key derived from the preview and sent as the broker client_order_id. UNIQUE so a '
    'retry after an ambiguous timeout collides here even if the first request never reached Alpaca.';
COMMENT ON COLUMN orders.preview IS
    'The preview the operator approved, verbatim. Regenerating it later from current prices would '
    'answer a different question than "what did they agree to".';
COMMENT ON COLUMN orders.submit_status IS
    '"unknown" is a real, expected state: the broker call timed out and whether it landed is not '
    'known. It must not be collapsed into rejected — one means no order exists, the other means '
    'nobody knows yet, and only reconciliation can tell them apart.';

CREATE INDEX ix_orders_submitted ON orders (submitted_at DESC);
CREATE INDEX ix_orders_symbol_time ON orders (symbol, submitted_at DESC);
CREATE INDEX ix_orders_operator ON orders (operator_id, submitted_at DESC) WHERE operator_id IS NOT NULL;
-- Finding orders that still need reconciling is the job that runs most often.
CREATE INDEX ix_orders_unreconciled ON orders (submitted_at) WHERE reconciled_at IS NULL;

-- ── Arming (design §2.3) ──────────────────────────────────────────────────────────────────────
-- Arming is a distinct, audited, expiring act rather than a property of a session: holding a login
-- must not be sufficient to trade. Stored rather than in-process so a backend restart cannot leave
-- execution silently armed, and so "who armed this, and when" survives the process.
CREATE TABLE execution_arming (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    armed_by      bigint      REFERENCES operators(id) ON DELETE SET NULL,
    armed_at      timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    -- Set on explicit disarm. NULL and unexpired == currently armed.
    disarmed_at   timestamptz,
    disarmed_by   bigint      REFERENCES operators(id) ON DELETE SET NULL,
    -- Why it ended: 'manual' | 'expired' | 'rate_cap' (design §1.5 trips disarm).
    disarm_reason text,
    CONSTRAINT ck_arming_window CHECK (expires_at > armed_at),
    CONSTRAINT ck_arming_disarm_paired CHECK (
        (disarmed_at IS NULL AND disarm_reason IS NULL)
        OR (disarmed_at IS NOT NULL AND disarm_reason IS NOT NULL)
    )
);

COMMENT ON TABLE execution_arming IS
    'One row per arming window. Persisted rather than in-process so a restart cannot leave execution '
    'silently armed and so the actor survives the process. NULL disarmed_at + expires_at in the '
    'future == armed.';

CREATE INDEX ix_arming_live ON execution_arming (expires_at DESC) WHERE disarmed_at IS NULL;

-- ── Grants ────────────────────────────────────────────────────────────────────────────────────
-- rh_app owns these: execution runs on the application pool, not the auth pool. rh_auth gets
-- nothing — it exists to read credentials and has no business touching orders (migration 012's
-- separation, in the other direction).
GRANT SELECT, INSERT, UPDATE ON orders TO rh_app;
GRANT SELECT, INSERT, UPDATE ON execution_arming TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE orders_id_seq TO rh_app;
GRANT USAGE, SELECT ON SEQUENCE execution_arming_id_seq TO rh_app;

-- No DELETE, deliberately. An audit trail that can be deleted by the process it audits is not one.
REVOKE DELETE ON orders FROM rh_app;
REVOKE DELETE ON execution_arming FROM rh_app;
