-- 012_auth_schema (down) — remove the auth tables and the rh_auth role.
--
-- Data loss: every operator account, session, TOTP enrolment, recovery code, challenge,
-- verification token, and auth audit event. Marked destructive in the FILENAME (ADR-002).
-- Reverse order of creation so children go before operators, the parent of every FK here.

DROP TRIGGER IF EXISTS trg_operator_totp_updated_at ON operator_totp;
DROP TRIGGER IF EXISTS trg_operators_updated_at ON operators;

DROP TABLE IF EXISTS auth_events;
DROP TABLE IF EXISTS email_verification_tokens;
DROP TABLE IF EXISTS mfa_login_challenges;
DROP TABLE IF EXISTS recovery_codes;
DROP TABLE IF EXISTS operator_totp;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS operators;

-- Same pattern as 001's down for rh_app: the role must lose its grants before it can go.
-- rh_auth owns no objects (DDL runs as the migration role), so DROP OWNED only revokes, and only
-- for the current database — which is the complete set, since 012 granted in no other.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rh_auth') THEN
        DROP OWNED BY rh_auth;
        DROP ROLE rh_auth;
    END IF;
END
$$;

-- rh_app's REVOKEs need no reversal: the six revoked tables no longer exist, and default
-- privileges (001 as narrowed by 011) were never altered by 012 in either direction.
