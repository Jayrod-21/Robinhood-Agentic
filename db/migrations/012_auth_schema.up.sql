-- 012_auth_schema — operator accounts, sessions, TOTP, recovery codes, MFA challenges, email
-- verification tokens, the append-only auth audit log, and the second least-privilege role
-- `rh_auth` (docs/AUTH_THREAT_MODEL.md §4 persistence spec, §8 role split).
--
-- The role story, because it is the part that bites (§8):
--   * 011 narrowed the default grant so every table born here starts as SELECT, INSERT for
--     `rh_app`. For six of these seven tables even SELECT is too much — password hashes,
--     encrypted TOTP secrets, and recovery-code hashes must never be readable by the role every
--     non-auth query runs as, or any SQL injection anywhere in the app reads the entire auth
--     store. The REVOKE below is the load-bearing line of this migration.
--   * `auth_events` is the deliberate exception (§5.12): it carries no secrets by construction,
--     so `rh_app` KEEPS its inherited SELECT + INSERT — the merged catalog gate
--     (test_runner_db.py::test_append_only_tables_enumerated_from_catalog) requires exactly that
--     for every table marked 'APPEND-ONLY (enforced by grants)'. Residual, stated: an injection
--     running as rh_app could append misleading audit rows; it cannot alter or erase real ones,
--     and non-erasability is the property an audit log actually needs.
--   * `rh_auth` inherits nothing (001's default privileges name rh_app as grantee, and default
--     ACLs are per-grantee), so every grant it holds below is deliberate. Writes are
--     column-level, following the merged precedent in 004 (GRANT UPDATE (retired_at, …) ON
--     agents), never blanket UPDATE.
--
-- §7 constraint honoured here: `operator_totp.secret_encrypted` stores AES-256-GCM CIPHERTEXT
-- only, as base64(nonce ‖ tag ‖ ciphertext). The key (TOTP_SECRET_ENC_KEY) lives in backend/.env
-- and must never appear in this database — no key table, no decrypt function, nothing that would
-- need it in-database.
--
-- Conventions as 001: BIGINT identity surrogate keys, TIMESTAMPTZ always, TEXT with a CHECK,
-- named constraints, ix_/uq_/fk_/ck_ naming. The runner owns the transaction: no BEGIN/COMMIT.

-- ── operators (§4, §5.1) ─────────────────────────────────────────────────────────────────────
-- Accounts are seeded by the host CLI only (no signup route, invariant §11.1). Operators are
-- disabled, never deleted — disabled_at is the tombstone, matching securities.delisted_at.
CREATE TABLE IF NOT EXISTS operators (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email             TEXT        NOT NULL,
    -- Argon2id PHC string ($argon2id$v=…$…). The CHECK refuses anything that is not a PHC-format
    -- Argon2id hash, so a bug that reaches this column with plaintext fails loudly at INSERT.
    password_hash     TEXT        NOT NULL,
    email_verified_at TIMESTAMPTZ,
    disabled_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_operators_email         CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' AND length(email) <= 254),
    CONSTRAINT ck_operators_password_hash CHECK (password_hash ~ '^\$argon2id\$')
);

-- Case-insensitive uniqueness: two operators may not differ only by letter case.
CREATE UNIQUE INDEX IF NOT EXISTS uq_operators_email ON operators (lower(email));

DROP TRIGGER IF EXISTS trg_operators_updated_at ON operators;
CREATE TRIGGER trg_operators_updated_at
    BEFORE UPDATE ON operators
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE operators IS
    'Operator accounts (AUTH_THREAT_MODEL §4). Seeded by bin/manage_operator.py only — there is '
    'no signup route (§11.1). password_hash is an Argon2id PHC string; disabled_at is the '
    'tombstone (operators are disabled, never deleted).';

-- ── sessions (§5.3) ──────────────────────────────────────────────────────────────────────────
-- One row per issued __Host-rh_sid cookie. The database stores only the SHA-256 hex digest of
-- the 32-byte token — a dump of this table cannot be replayed as cookies. Rows are never mutated
-- to extend or transfer (invariant §11.5): the only in-place writes are last_seen_at (idle
-- tracking) and revoked_at (server-side logout/revocation), which is exactly the column-level
-- UPDATE rh_auth receives below.
CREATE TABLE IF NOT EXISTS sessions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator_id  BIGINT      NOT NULL,
    token_hash   TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ,
    user_agent   TEXT,
    ip           INET,

    CONSTRAINT fk_sessions_operator FOREIGN KEY (operator_id)
        REFERENCES operators (id) ON DELETE CASCADE,
    CONSTRAINT ck_sessions_token_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_sessions_expiry     CHECK (expires_at > created_at),
    CONSTRAINT ck_sessions_user_agent CHECK (user_agent IS NULL OR length(user_agent) <= 512)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_token_hash ON sessions (token_hash);
CREATE INDEX IF NOT EXISTS ix_sessions_operator ON sessions (operator_id);

COMMENT ON TABLE sessions IS
    'Server-side session store (§5.3). token_hash is the SHA-256 hex of the cookie value — never '
    'the value itself. Absolute expiry via expires_at, idle timeout via last_seen_at, both '
    'checked server-side. Rows are revoked (revoked_at), never rewritten or deleted in-band.';

-- ── operator_totp (§5.4, §7) ─────────────────────────────────────────────────────────────────
-- One row per operator (PK = operator_id). Also carries the §5.8 lockout state: the counter is
-- bumped only by failures at the TOTP step, which is reachable only after a correct password.
CREATE TABLE IF NOT EXISTS operator_totp (
    operator_id      BIGINT      PRIMARY KEY,
    -- AES-256-GCM ciphertext, base64(nonce ‖ tag ‖ ciphertext): 12-byte nonce + 16-byte tag +
    -- ciphertext ≥ 1 byte → ≥ 29 raw bytes → ≥ 40 base64 chars. The length floor means a raw
    -- base32 TOTP secret (32 chars) can never satisfy this column; the key lives OUTSIDE the
    -- database (§7) and nothing in this schema requires it in-database.
    secret_encrypted TEXT        NOT NULL,
    confirmed_at     TIMESTAMPTZ,
    -- Monotonic RFC-6238 high-water mark: a code's step must exceed this to verify (§5.4).
    last_used_step   BIGINT      NOT NULL DEFAULT 0,
    failed_attempts  INTEGER     NOT NULL DEFAULT 0,
    locked_until     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_operator_totp_operator FOREIGN KEY (operator_id)
        REFERENCES operators (id) ON DELETE CASCADE,
    CONSTRAINT ck_operator_totp_secret_b64 CHECK (secret_encrypted ~ '^[A-Za-z0-9+/]+={0,2}$'),
    CONSTRAINT ck_operator_totp_secret_len CHECK (length(secret_encrypted) >= 40),
    CONSTRAINT ck_operator_totp_counters   CHECK (last_used_step >= 0 AND failed_attempts >= 0)
);

DROP TRIGGER IF EXISTS trg_operator_totp_updated_at ON operator_totp;
CREATE TRIGGER trg_operator_totp_updated_at
    BEFORE UPDATE ON operator_totp
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE operator_totp IS
    'TOTP enrolment + §5.8 lockout state. secret_encrypted is AES-256-GCM ciphertext only '
    '(base64(nonce‖tag‖ciphertext)); the key is TOTP_SECRET_ENC_KEY in backend/.env and must '
    'never enter this database (§7). Login reads only rows with confirmed_at IS NOT NULL.';
COMMENT ON COLUMN operator_totp.secret_encrypted IS
    'Ciphertext, never plaintext and never the key. Length floor 40 means a bare base32 secret '
    'cannot be stored here by accident.';
COMMENT ON COLUMN operator_totp.last_used_step IS
    'Monotonic high-water mark of the last accepted RFC-6238 step; replay defense §5.4 — a code '
    'whose step is not greater is refused, written in the same transaction that issues the session.';

-- ── recovery_codes (§5.5) ────────────────────────────────────────────────────────────────────
-- SHA-256 hex digests of 50-bit Crockford-base32 codes; plaintext is shown once at generation
-- and never persisted. Single-use is a rowcount-gated UPDATE of used_at; regeneration and TOTP
-- re-enrolment remove the old set outright (hence rh_auth's DELETE below).
CREATE TABLE IF NOT EXISTS recovery_codes (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator_id BIGINT      NOT NULL,
    code_hash   TEXT        NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_recovery_codes_operator FOREIGN KEY (operator_id)
        REFERENCES operators (id) ON DELETE CASCADE,
    CONSTRAINT ck_recovery_codes_hash CHECK (code_hash ~ '^[0-9a-f]{64}$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_codes_hash ON recovery_codes (code_hash);
CREATE INDEX IF NOT EXISTS ix_recovery_codes_operator ON recovery_codes (operator_id);

COMMENT ON TABLE recovery_codes IS
    'SHA-256 digests of single-use recovery codes (§5.5 — SHA-256 is correct HERE because the '
    'codes carry 50 bits of CSPRNG entropy; the reasoning does not transfer to password_hash). '
    'Consumption is UPDATE … SET used_at WHERE used_at IS NULL, gated on rowcount = 1.';

-- ── mfa_login_challenges (§4, §5.4) ──────────────────────────────────────────────────────────
-- The only pre-auth artifact: minted by the password step, single-use, short-lived, and
-- purpose-scoped — an ''enroll'' challenge is invisible to the login route and vice versa
-- (lookups filter on purpose). Confers nothing but the right to attempt the second step.
CREATE TABLE IF NOT EXISTS mfa_login_challenges (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator_id BIGINT      NOT NULL,
    token_hash  TEXT        NOT NULL,
    purpose     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    attempts    INTEGER     NOT NULL DEFAULT 0,

    CONSTRAINT fk_mfa_challenges_operator FOREIGN KEY (operator_id)
        REFERENCES operators (id) ON DELETE CASCADE,
    CONSTRAINT ck_mfa_challenges_token_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_mfa_challenges_purpose    CHECK (purpose IN ('login', 'enroll')),
    CONSTRAINT ck_mfa_challenges_expiry     CHECK (expires_at > created_at),
    CONSTRAINT ck_mfa_challenges_attempts   CHECK (attempts >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mfa_challenges_token_hash ON mfa_login_challenges (token_hash);
CREATE INDEX IF NOT EXISTS ix_mfa_challenges_operator ON mfa_login_challenges (operator_id);

COMMENT ON TABLE mfa_login_challenges IS
    'Single-use, purpose-scoped challenge tokens minted by the password step (§4). token_hash is '
    'the SHA-256 hex of the token; purpose scoping is why an enroll challenge cannot complete a '
    'login (§5.4). Consumption is rowcount-gated on consumed_at IS NULL.';

-- ── email_verification_tokens (§5.6) ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator_id    BIGINT      NOT NULL,
    token_hash     TEXT        NOT NULL,
    -- The address this token ATTESTS. Consumption requires it to equal the operator''s current
    -- address — the binding that defuses the A → B → A stale-verification replay (§5.6), and it
    -- holds even if a supersession is ever lost to a crash.
    email          TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    consumed_at    TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,

    CONSTRAINT fk_evt_operator FOREIGN KEY (operator_id)
        REFERENCES operators (id) ON DELETE CASCADE,
    CONSTRAINT ck_evt_token_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evt_email      CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' AND length(email) <= 254),
    CONSTRAINT ck_evt_expiry     CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evt_token_hash ON email_verification_tokens (token_hash);
CREATE INDEX IF NOT EXISTS ix_evt_operator ON email_verification_tokens (operator_id);
-- "Exactly one live verification token per operator" (§5.6) enforced structurally, not by
-- application discipline: a new token can only be inserted once every prior live row is
-- consumed or invalidated, even under concurrent resends.
CREATE UNIQUE INDEX IF NOT EXISTS uq_evt_one_live_per_operator ON email_verification_tokens (operator_id)
    WHERE consumed_at IS NULL AND invalidated_at IS NULL;

COMMENT ON TABLE email_verification_tokens IS
    'Email-channel proof tokens (§5.6): SHA-256 digest only, 24h TTL, at most one live token per '
    'operator (partial unique index). email is the address the token attests — consumption '
    'requires it to still be the operator''s current address. Redemption confers no session.';

-- ── auth_events (§5.12) ──────────────────────────────────────────────────────────────────────
-- The audit log. Its value is deferred by design: near-worthless while the app is read-only,
-- load-bearing the day the order path ships — do not remove it as unused (§5.12, §3.2).
CREATE TABLE IF NOT EXISTS auth_events (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Nullable: failed logins against unknown emails still log an event, with no operator to
    -- point at. ON DELETE SET NULL so audit history outlives any (out-of-band) operator removal.
    operator_id BIGINT,
    event_type  TEXT        NOT NULL,
    outcome     TEXT        NOT NULL,
    ip          INET,
    user_agent  TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_auth_events_operator FOREIGN KEY (operator_id)
        REFERENCES operators (id) ON DELETE SET NULL,
    CONSTRAINT ck_auth_events_event_type CHECK (event_type ~ '^[a-z0-9_]{2,64}$'),
    CONSTRAINT ck_auth_events_outcome    CHECK (outcome ~ '^[a-z0-9_]{2,32}$'),
    CONSTRAINT ck_auth_events_user_agent CHECK (user_agent IS NULL OR length(user_agent) <= 512)
);

CREATE INDEX IF NOT EXISTS ix_auth_events_operator_time
    ON auth_events (operator_id, occurred_at DESC) WHERE operator_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_auth_events_time ON auth_events (occurred_at DESC);

-- The marker is load-bearing: test_append_only_tables_enumerated_from_catalog discovers this
-- table by it and asserts rh_app holds no UPDATE/DELETE — and still holds SELECT + INSERT.
COMMENT ON TABLE auth_events IS
    'Who did what, from where, and how it came out (§5.12): login/TOTP success+failure, lockout, '
    'recovery-code use, logout, email change, enrolment, session revocation. Emails, tokens, '
    'codes, and secrets are NEVER written here, which is why rh_app keeps its inherited read. '
    'APPEND-ONLY (enforced by grants): 011''s narrowed defaults mean no role in the app can '
    'rewrite or erase history — misleading rows can only be appended, never removed.';

-- ── roles and grants (§8 — the load-bearing part) ────────────────────────────────────────────
-- Same catalog-check pattern as 001: CREATE ROLE has no IF NOT EXISTS in PG16. Created with no
-- password; it cannot authenticate over the network until an operator sets one
-- (bin/db_psql.sh -c "ALTER ROLE rh_auth WITH PASSWORD '…'").
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rh_auth') THEN
        CREATE ROLE rh_auth LOGIN;
    END IF;
END
$$;

COMMENT ON ROLE rh_auth IS
    'Auth-path runtime role (AUTH_THREAT_MODEL §8): the backend''s second pool '
    '(AUTH_DATABASE_URL) connects as this. Holds column-level grants on exactly what the auth '
    'flows touch and nothing on market data. Like rh_app (see its comment), in-container '
    'loopback/socket connections are `trust`, scram-sha-256 applies elsewhere.';

GRANT USAGE ON SCHEMA public TO rh_auth;

-- §8 step 1 — THE load-bearing line. 011 left new tables born SELECT, INSERT for rh_app; for the
-- secret-bearing auth tables even SELECT is an amplification (any injection in any non-auth
-- query would read every password hash, encrypted TOTP secret, and recovery-code digest).
-- auth_events is deliberately absent from this list (§5.12).
REVOKE ALL PRIVILEGES ON operators, sessions, operator_totp, recovery_codes,
    mfa_login_challenges, email_verification_tokens FROM rh_app;
-- Their identity sequences too: 001's default ACL granted rh_app USAGE, SELECT on every new
-- sequence, and sequence last_value is a row-count side channel rh_app has no business reading.
-- auth_events_id_seq stays granted — rh_app keeps INSERT on auth_events.
REVOKE ALL PRIVILEGES ON SEQUENCE operators_id_seq, sessions_id_seq, recovery_codes_id_seq,
    mfa_login_challenges_id_seq, email_verification_tokens_id_seq FROM rh_app;

-- §8 step 2 — rh_auth's narrow set, per flow. Default ACLs named rh_app only, so rh_auth starts
-- from nothing and everything below is the complete list. Writes are column-level (the 004
-- precedent); a flow that needs a new column must widen the grant here, in a migration, on
-- purpose.
--
-- operators: login + /me read the row; email change writes email (and clears the verified
-- stamp); token consumption stamps email_verified_at. Creation/disable/password reset are CLI
-- surfaces running as the DDL role — rh_auth cannot mint or remove accounts.
GRANT SELECT ON operators TO rh_auth;
GRANT UPDATE (email, email_verified_at) ON operators TO rh_auth;

-- sessions: issued after the TOTP step (INSERT), validated per-request (SELECT), idle-tracked
-- and revoked in place (the only two mutable columns, invariant §11.5). No DELETE: revocation
-- is a stamp, and erasing session history is nobody's runtime job.
GRANT SELECT, INSERT ON sessions TO rh_auth;
GRANT UPDATE (last_seen_at, revoked_at) ON sessions TO rh_auth;
GRANT USAGE, SELECT ON SEQUENCE sessions_id_seq TO rh_auth;

-- operator_totp: enrolment inserts the pending row; confirm stamps confirmed_at; re-enrolment
-- overwrites secret_encrypted (and resets confirmed_at) in place; verification maintains the
-- §5.4 step high-water mark and the §5.8 lockout counters. operator_id/created_at stay
-- immutable to this role, and the row itself cannot be removed by it (CLI reset-totp runs as
-- the DDL role).
GRANT SELECT, INSERT ON operator_totp TO rh_auth;
GRANT UPDATE (secret_encrypted, confirmed_at, last_used_step, failed_attempts, locked_until)
    ON operator_totp TO rh_auth;

-- recovery_codes: generation inserts, §5.5 consumption is the rowcount-gated UPDATE of used_at,
-- and regeneration / TOTP re-enrolment removes the old set outright (used or unused) in the same
-- transaction — the one legitimate runtime DELETE in this schema.
GRANT SELECT, INSERT, DELETE ON recovery_codes TO rh_auth;
GRANT UPDATE (used_at) ON recovery_codes TO rh_auth;
GRANT USAGE, SELECT ON SEQUENCE recovery_codes_id_seq TO rh_auth;

-- mfa_login_challenges: the password step inserts, the TOTP step looks up (filtered on
-- purpose), bumps attempts, and consumes — rowcount-gated on consumed_at.
GRANT SELECT, INSERT ON mfa_login_challenges TO rh_auth;
GRANT UPDATE (consumed_at, attempts) ON mfa_login_challenges TO rh_auth;
GRANT USAGE, SELECT ON SEQUENCE mfa_login_challenges_id_seq TO rh_auth;

-- email_verification_tokens: issue inserts (superseding prior live tokens via invalidated_at),
-- consumption stamps consumed_at. token_hash/email/expires_at are immutable once written — a
-- token cannot be re-pointed at a different address (§5.6).
GRANT SELECT, INSERT ON email_verification_tokens TO rh_auth;
GRANT UPDATE (consumed_at, invalidated_at) ON email_verification_tokens TO rh_auth;
GRANT USAGE, SELECT ON SEQUENCE email_verification_tokens_id_seq TO rh_auth;

-- auth_events: write-only for the auth path. No SELECT — no auth flow reads its own audit log
-- (lockout state lives on operator_totp), and a narrower grant is a smaller blast radius.
GRANT INSERT ON auth_events TO rh_auth;
GRANT USAGE, SELECT ON SEQUENCE auth_events_id_seq TO rh_auth;
