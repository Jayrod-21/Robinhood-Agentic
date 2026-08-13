-- 010_rh_app_comment — correct the untrue authentication claim about rh_app (issue #31).
--
-- 001_core_schema.up.sql claims the passwordless rh_app role "cannot authenticate until an
-- operator sets one". That is FALSE for the paths that matter most: the postgres:16-alpine
-- image's default pg_hba is `trust` for the in-container unix socket and loopback (127.0.0.1,
-- ::1), so ANY role with LOGIN — rh_app included — authenticates from inside the container with
-- no password at all. Verified live against pg_hba_file_rules on rh-db (2026-08-13): local/all
-- trust, host 127.0.0.1 + ::1 trust, host all-others scram-sha-256. bin/db_psql.sh documents the
-- same truth. The claim IS true for every other source: under scram, a role with no stored
-- password cannot authenticate until an operator sets one.
--
-- Real exposure is nil — anyone who can exec into the container already reaches the `rh`
-- superuser the same way, and the database publishes no host port (ADR-001) — but an untrue
-- security comment is exactly the class of defect the infra review made five blockers out of.
-- 001 is applied and checksum-locked (editing it raises ChecksumMismatch by design), so the
-- correction lives in the catalog as a COMMENT ON ROLE: the durable, queryable description of
-- the role now states the verified truth even though 001's file text cannot change.

COMMENT ON ROLE rh_app IS
    'Runtime role: DML only, no DDL, not superuser (001_core_schema). Created with NO stored '
    'password. AUTHENTICATION TRUTH (corrects the untrue claim in 001''s file comment, issue '
    '#31; verified against pg_hba_file_rules 2026-08-13): the image''s default pg_hba trusts the '
    'in-container unix socket and loopback (127.0.0.1, ::1), so rh_app CAN authenticate from '
    'inside the container with no password. From every other source pg_hba requires '
    'scram-sha-256, where a passwordless role cannot authenticate until an operator sets one '
    '(bin/db_psql.sh -c "ALTER ROLE rh_app WITH PASSWORD ''…''"). Exposure is nil in practice: '
    'the in-container path already reaches the rh superuser, and no host port is published '
    '(ADR-001).';
