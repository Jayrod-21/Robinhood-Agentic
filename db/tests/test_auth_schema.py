"""012_auth_schema against a real throwaway Postgres: the §8 role split, proven behaviorally.

The property under test (docs/AUTH_THREAT_MODEL.md §8): the auth tables are NOT readable by
`rh_app` — the role every existing query runs as — so an injection or over-broad SELECT anywhere
in the non-auth app cannot reach password hashes, encrypted TOTP secrets, or recovery-code
digests. A second role, `rh_auth`, holds column-level grants on exactly what each auth flow
touches and nothing else. `auth_events` is the deliberate exception (§5.12): it carries no
secrets, keeps rh_app's inherited SELECT+INSERT, and buys the append-only erasure guarantee via
the merged catalog marker gate instead of a per-table REVOKE.

Assertions come in pairs on purpose: a catalog probe (has_table_privilege — what the ACL says)
AND a behavioral probe (SET ROLE + the actual statement — what the server does). The catalog
probe localizes a regression; the behavioral probe is the property itself.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

from migrate import EXIT_OK
from migrate import main as migrate_main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PG_IMAGE = "postgres:16-alpine"

# The six tables §8 step 1 strips from rh_app entirely. auth_events is deliberately absent.
SECRET_AUTH_TABLES = (
    "operators",
    "sessions",
    "operator_totp",
    "recovery_codes",
    "mfa_login_challenges",
    "email_verification_tokens",
)

# Valid-by-CHECK fixture values (Argon2id PHC prefix, SHA-256 hex, base64 ≥ 40 chars).
#
# The parameters mirror crypto.PASSWORD_HASHER (m=65536, t=3, p=1) even though nothing here ever
# verifies this string — the CHECK only looks at the "$argon2id$" prefix, so any shape-valid value
# would pass. It said p=4 until the timing-oracle fix: argon2-cffi's default, which is exactly the
# drift that made unknown-email logins 2.5x slower than known ones. A fixture that models the wrong
# parameters is a wrong answer waiting for someone to copy it.
PHC = "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$b2s"
CIPHERTEXT = "QUJDREVGR0g" * 5  # 55 base64 chars — past the nonce+tag length floor


def sha_hex(tag: str) -> str:
    """A distinct, shape-valid 64-char lowercase hex digest per tag."""
    import hashlib

    return hashlib.sha256(tag.encode()).hexdigest()


@pytest.fixture(scope="session")
def auth_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


def _admin_url(pg: PostgresContainer) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


@pytest.fixture
def db(auth_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, fully-migrated database per test, exported as DATABASE_URL for the runner."""
    name = f"adb_{uuid.uuid4().hex[:12]}"
    admin = _admin_url(auth_pg)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert migrate_main(["up", "--migrations-dir", str(REPO_MIGRATIONS)]) == EXIT_OK
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def q(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


class RoleSession:
    """An autocommit superuser connection downgraded via SET ROLE — behavioral privilege probes.

    SET ROLE (not a re-login) because the container's pg_hba requires a password and the runtime
    roles are deliberately created without one; privilege checks apply to the CURRENT role either
    way, so the probe is faithful.
    """

    def __init__(self, url: str, role: str) -> None:
        self.conn = psycopg.connect(url, autocommit=True)
        self.conn.execute(f'SET ROLE "{role}"')

    def ok(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self.conn.execute(sql, params)
        return cur.fetchall() if cur.description else []

    def refused(self, sql: str, params: tuple = ()) -> None:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(sql, params)

    def close(self) -> None:
        self.conn.close()


def _seed_operator(db: str, email: str = "jared@example.com") -> int:
    """Accounts are a CLI/DDL-role surface (§11.1) — seeded as admin, exactly like production."""
    return q(
        db,
        "INSERT INTO operators (email, password_hash) VALUES (%s, %s) RETURNING id",
        (email, PHC),
    )[0][0]


# ── rh_app: the REVOKE actually ran ──────────────────────────────────────────────────────────


def test_rh_app_holds_nothing_on_any_secret_auth_table(db: str) -> None:
    """§8 step 1 — the load-bearing REVOKE. 011's defaults grant SELECT+INSERT at CREATE time, so
    silently omitting the REVOKE would leave every secret column readable by the role the whole
    non-auth app runs as, and nothing would complain. Catalog first, then the server itself."""
    for table in SECRET_AUTH_TABLES:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            held = q(db, "SELECT has_table_privilege('rh_app', %s, %s)", (table, priv))[0][0]
            assert held is False, f"rh_app must hold no {priv} on {table} — the 012 REVOKE is missing"
        # A column-level grant would not show in has_table_privilege; close that back door too.
        assert q(
            db, "SELECT has_any_column_privilege('rh_app', %s, 'SELECT')", (table,)
        )[0][0] is False, f"rh_app holds a column-level SELECT on {table}"

    # No side channel through the identity sequences either (last_value ≈ row counts).
    for seq in (
        "operators_id_seq",
        "sessions_id_seq",
        "recovery_codes_id_seq",
        "mfa_login_challenges_id_seq",
        "email_verification_tokens_id_seq",
    ):
        for priv in ("USAGE", "SELECT"):
            held = q(db, "SELECT has_sequence_privilege('rh_app', %s, %s)", (seq, priv))[0][0]
            assert held is False, f"rh_app must not hold {priv} on {seq}"

    # Behavioral: the four probes §8 names, as the server enforces them.
    op = _seed_operator(db)
    q(db, "INSERT INTO operator_totp (operator_id, secret_encrypted) VALUES (%s, %s)", (op, CIPHERTEXT))
    q(db, "INSERT INTO recovery_codes (operator_id, code_hash) VALUES (%s, %s)", (op, sha_hex("rc")))
    q(
        db,
        "INSERT INTO sessions (operator_id, token_hash, expires_at) VALUES (%s, %s, now() + interval '1 day')",
        (op, sha_hex("sid")),
    )
    app = RoleSession(db, "rh_app")
    try:
        app.refused("SELECT password_hash FROM operators")
        app.refused("SELECT secret_encrypted FROM operator_totp")
        app.refused("SELECT code_hash FROM recovery_codes")
        app.refused("SELECT token_hash FROM sessions")
        # And it cannot write auth state into existence either.
        app.refused("INSERT INTO operators (email, password_hash) VALUES ('x@y.zz', %s)", (PHC,))
    finally:
        app.close()


def test_auth_events_keeps_select_insert_for_rh_app_and_stays_append_only(db: str) -> None:
    """§5.12's resolution of the marker-vs-REVOKE conflict: auth_events carries the exact
    'APPEND-ONLY (enforced by grants)' marker, so the merged catalog gate demands rh_app keep
    SELECT+INSERT ("append-only must still allow appends") — and audit rows carry no secrets by
    construction, so the read is harmless. The residual (§5.12): rh_app can append misleading
    events; it can never alter or erase real ones."""
    comment = q(db, "SELECT obj_description('auth_events'::regclass, 'pg_class')")[0][0]
    assert "APPEND-ONLY (enforced by grants)" in comment, (
        "auth_events lost the exact catalog marker the merged enumeration gate keys on"
    )

    for priv, expected in (("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False)):
        held = q(db, "SELECT has_table_privilege('rh_app', 'auth_events', %s)", (priv,))[0][0]
        assert held is expected, f"rh_app {priv} on auth_events should be {expected}"
    # No column-level UPDATE backdoor for either runtime role.
    for role in ("rh_app", "rh_auth"):
        assert q(db, "SELECT has_any_column_privilege(%s, 'auth_events', 'UPDATE')", (role,))[0][0] is False

    app = RoleSession(db, "rh_app")
    try:
        app.ok("INSERT INTO auth_events (event_type, outcome) VALUES ('login', 'failure')")
        assert app.ok("SELECT count(*) FROM auth_events")[0][0] == 1
        app.refused("UPDATE auth_events SET outcome = 'success' WHERE outcome = 'failure'")
        app.refused("DELETE FROM auth_events")
    finally:
        app.close()


# ── rh_auth: exactly what each flow needs, and no more ───────────────────────────────────────


def test_rh_auth_holds_exactly_the_named_grants(db: str) -> None:
    """The complete privilege matrix, asserted as an equality — not membership — so an over-grant
    fails as loudly as a missing one. Default ACLs name rh_app only (001), so everything rh_auth
    holds was granted by 012 on purpose; this pins that set."""
    expected_table = {
        "operators": {"SELECT"},
        "sessions": {"SELECT", "INSERT"},
        "operator_totp": {"SELECT", "INSERT"},
        "recovery_codes": {"SELECT", "INSERT", "DELETE"},
        "mfa_login_challenges": {"SELECT", "INSERT"},
        "email_verification_tokens": {"SELECT", "INSERT"},
        "auth_events": {"INSERT"},
    }
    for table, wanted in expected_table.items():
        held = {
            priv
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if q(db, "SELECT has_table_privilege('rh_auth', %s, %s)", (table, priv))[0][0]
        }
        assert held == wanted, f"rh_auth table-level privileges on {table}: {held} != {wanted}"

    # Column-level UPDATE grants: exactly the lifecycle columns each flow writes (§8 step 2,
    # the 004 precedent). rh_auth holds no table-level UPDATE anywhere, so every row in
    # column_privileges with privilege UPDATE is one of these deliberate grants.
    expected_update_cols = {
        "operators": {"email", "email_verified_at"},
        "sessions": {"last_seen_at", "revoked_at"},
        "operator_totp": {"secret_encrypted", "confirmed_at", "last_used_step", "failed_attempts", "locked_until"},
        "recovery_codes": {"used_at"},
        "mfa_login_challenges": {"consumed_at", "attempts"},
        "email_verification_tokens": {"consumed_at", "invalidated_at"},
    }
    rows = q(
        db,
        "SELECT table_name, column_name FROM information_schema.column_privileges "
        "WHERE grantee = 'rh_auth' AND privilege_type = 'UPDATE'",
    )
    granted: dict[str, set[str]] = {}
    for table, column in rows:
        granted.setdefault(table, set()).add(column)
    assert granted == expected_update_cols, f"rh_auth UPDATE column grants drifted: {granted}"

    # Nothing at all on the market-data side of the house.
    for table in ("securities", "data_sources", "price_bars_daily", "evaluation_runs", "agents"):
        assert q(
            db, "SELECT has_any_column_privilege('rh_auth', %s, 'SELECT')", (table,)
        )[0][0] is False, f"rh_auth must hold nothing on market-data table {table}"


def test_rh_auth_can_walk_every_flow_and_nothing_else(db: str) -> None:
    """Behavioral proof in both directions: every statement the §5 flows issue succeeds as
    rh_auth, and the adjacent statement each flow must NOT be able to issue is refused."""
    op = _seed_operator(db)
    auth = RoleSession(db, "rh_auth")
    try:
        # Login step 1: read the credential row (§5.1) — the read rh_app just lost.
        assert auth.ok("SELECT id, password_hash, disabled_at FROM operators WHERE id = %s", (op,))[0][0] == op
        # …and mint a purpose-scoped challenge (§4).
        auth.ok(
            "INSERT INTO mfa_login_challenges (operator_id, token_hash, purpose, expires_at) "
            "VALUES (%s, %s, 'login', now() + interval '5 minutes')",
            (op, sha_hex("ch1")),
        )
        # TOTP enrolment (§5.4): pending secret in, confirm, then verify maintains the
        # high-water mark and lockout counters.
        auth.ok("INSERT INTO operator_totp (operator_id, secret_encrypted) VALUES (%s, %s)", (op, CIPHERTEXT))
        auth.ok("UPDATE operator_totp SET confirmed_at = now(), secret_encrypted = %s WHERE operator_id = %s", (CIPHERTEXT, op))
        auth.ok(
            "UPDATE operator_totp SET last_used_step = 57000000, failed_attempts = 0, locked_until = NULL "
            "WHERE operator_id = %s",
            (op,),
        )
        # Challenge consumption is the rowcount-gated single-use UPDATE (§5.4/§5.5 pattern).
        assert auth.conn.execute(
            "UPDATE mfa_login_challenges SET consumed_at = now(), attempts = attempts + 1 "
            "WHERE token_hash = %s AND consumed_at IS NULL",
            (sha_hex("ch1"),),
        ).rowcount == 1
        # Session issue + idle tracking + server-side revocation (§5.3).
        auth.ok(
            "INSERT INTO sessions (operator_id, token_hash, expires_at) VALUES (%s, %s, now() + interval '14 days')",
            (op, sha_hex("s1")),
        )
        auth.ok("UPDATE sessions SET last_seen_at = now() WHERE token_hash = %s", (sha_hex("s1"),))
        auth.ok("UPDATE sessions SET revoked_at = now() WHERE token_hash = %s", (sha_hex("s1"),))
        # Recovery codes (§5.5): issue, single-use consume, regenerate-invalidates-outright.
        auth.ok("INSERT INTO recovery_codes (operator_id, code_hash) VALUES (%s, %s)", (op, sha_hex("rc1")))
        assert auth.conn.execute(
            "UPDATE recovery_codes SET used_at = now() WHERE code_hash = %s AND used_at IS NULL",
            (sha_hex("rc1"),),
        ).rowcount == 1
        auth.ok("DELETE FROM recovery_codes WHERE operator_id = %s", (op,))
        # Email verification (§5.6): issue, consume, supersede; stamp the operator verified;
        # email change writes the address.
        auth.ok(
            "INSERT INTO email_verification_tokens (operator_id, token_hash, email, expires_at) "
            "VALUES (%s, %s, 'jared@example.com', now() + interval '24 hours')",
            (op, sha_hex("v1")),
        )
        auth.ok("UPDATE email_verification_tokens SET invalidated_at = now() WHERE token_hash = %s", (sha_hex("v1"),))
        auth.ok("UPDATE operators SET email_verified_at = now() WHERE id = %s", (op,))
        auth.ok("UPDATE operators SET email = 'jared2@example.com', email_verified_at = NULL WHERE id = %s", (op,))
        # Every outcome writes an audit event (§5.12) — write-only for this role.
        auth.ok(
            "INSERT INTO auth_events (operator_id, event_type, outcome, ip) VALUES (%s, 'totp_login', 'success', '127.0.0.1')",
            (op,),
        )

        # ── and no more ──
        auth.refused("INSERT INTO operators (email, password_hash) VALUES ('mallory@example.com', %s)", (PHC,))
        auth.refused("DELETE FROM operators WHERE id = %s", (op,))
        auth.refused("UPDATE operators SET password_hash = %s WHERE id = %s", (PHC, op))  # CLI-only surface
        auth.refused("UPDATE operators SET disabled_at = now() WHERE id = %s", (op,))
        auth.refused("DELETE FROM sessions WHERE operator_id = %s", (op,))
        auth.refused("UPDATE sessions SET token_hash = %s WHERE operator_id = %s", (sha_hex("forged"), op))
        auth.refused("UPDATE sessions SET expires_at = now() + interval '10 years' WHERE operator_id = %s", (op,))
        auth.refused("DELETE FROM operator_totp WHERE operator_id = %s", (op,))  # reset-totp is CLI-only
        auth.refused("UPDATE mfa_login_challenges SET token_hash = %s WHERE operator_id = %s", (sha_hex("x"), op))
        auth.refused("UPDATE mfa_login_challenges SET purpose = 'enroll' WHERE operator_id = %s", (op,))
        auth.refused("DELETE FROM mfa_login_challenges WHERE operator_id = %s", (op,))
        auth.refused("UPDATE email_verification_tokens SET email = 'other@example.com' WHERE operator_id = %s", (op,))
        auth.refused("DELETE FROM email_verification_tokens WHERE operator_id = %s", (op,))
        auth.refused("SELECT count(*) FROM auth_events")  # write-only audit
        auth.refused("UPDATE auth_events SET outcome = 'success' WHERE operator_id = %s", (op,))
        auth.refused("DELETE FROM auth_events WHERE operator_id = %s", (op,))
        auth.refused("SELECT count(*) FROM securities")  # nothing on market data
    finally:
        auth.close()


# ── schema shape: the transactional guarantees §8 cites as the reason auth lives in Postgres ──


def test_auth_schema_shape_constraints(db: str) -> None:
    """CHECKs refuse the mis-stored shapes that would silently gut a defense: plaintext where a
    hash belongs, a bare base32 secret where ciphertext belongs, junk where a digest belongs."""
    op = _seed_operator(db)

    with pytest.raises(psycopg.errors.CheckViolation):
        q(db, "INSERT INTO operators (email, password_hash) VALUES ('a@b.cc', 'hunter2')")
    with pytest.raises(psycopg.errors.CheckViolation):
        q(db, "INSERT INTO operators (email, password_hash) VALUES ('not-an-email', %s)", (PHC,))
    with pytest.raises(psycopg.errors.UniqueViolation):  # case-insensitive uniqueness
        q(db, "INSERT INTO operators (email, password_hash) VALUES ('JARED@EXAMPLE.COM', %s)", (PHC,))
    with pytest.raises(psycopg.errors.CheckViolation):  # a raw base32 TOTP secret is too short
        q(db, "INSERT INTO operator_totp (operator_id, secret_encrypted) VALUES (%s, 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')", (op,))
    with pytest.raises(psycopg.errors.CheckViolation):  # token digests are 64 lowercase hex
        q(db, "INSERT INTO sessions (operator_id, token_hash, expires_at) VALUES (%s, 'deadbeef', now() + interval '1 day')", (op,))
    with pytest.raises(psycopg.errors.CheckViolation):  # purposes are a closed set (§5.4 scoping)
        q(
            db,
            "INSERT INTO mfa_login_challenges (operator_id, token_hash, purpose, expires_at) "
            "VALUES (%s, %s, 'password_reset', now() + interval '5 minutes')",
            (op, sha_hex("p")),
        )


def test_exactly_one_live_verification_token_per_operator(db: str) -> None:
    """§5.6/§8: 'exactly one live verification token per operator' is structural (a partial
    unique index), not application discipline — it holds under crashed supersessions and
    concurrent resends because the database refuses the second live row."""
    op = _seed_operator(db)
    q(
        db,
        "INSERT INTO email_verification_tokens (operator_id, token_hash, email, expires_at) "
        "VALUES (%s, %s, 'jared@example.com', now() + interval '24 hours')",
        (op, sha_hex("t1")),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        q(
            db,
            "INSERT INTO email_verification_tokens (operator_id, token_hash, email, expires_at) "
            "VALUES (%s, %s, 'jared@example.com', now() + interval '24 hours')",
            (op, sha_hex("t2")),
        )
    # Superseding the live token re-opens the slot; consuming does too.
    q(db, "UPDATE email_verification_tokens SET invalidated_at = now() WHERE token_hash = %s", (sha_hex("t1"),))
    q(
        db,
        "INSERT INTO email_verification_tokens (operator_id, token_hash, email, expires_at) "
        "VALUES (%s, %s, 'jared@example.com', now() + interval '24 hours')",
        (op, sha_hex("t2")),
    )


def test_down_removes_the_auth_surface_and_role_cleanly(db: str) -> None:
    """The destructive down (proven here on the throwaway container, never live): tables and the
    rh_auth role are gone after rolling back to 011, and re-applying 012 restores both — the
    up → down → up contract every migration in this repo honours."""
    md = str(REPO_MIGRATIONS)
    assert migrate_main(["down", "--allow-destructive", "--target", "011", "--migrations-dir", md]) == EXIT_OK
    for table in (*SECRET_AUTH_TABLES, "auth_events"):
        assert q(db, "SELECT to_regclass(%s)", (f"public.{table}",))[0][0] is None, f"{table} survived the down"
    assert q(db, "SELECT count(*) FROM pg_roles WHERE rolname = 'rh_auth'")[0][0] == 0
    # rh_app and its market-data grants are untouched by the rollback.
    assert q(db, "SELECT has_table_privilege('rh_app', 'securities', 'SELECT')")[0][0] is True

    assert migrate_main(["up", "--migrations-dir", md]) == EXIT_OK
    assert q(db, "SELECT to_regclass('public.operators')")[0][0] == "operators"
    assert q(db, "SELECT has_table_privilege('rh_app', 'operators', 'SELECT')")[0][0] is False
    assert q(db, "SELECT has_table_privilege('rh_auth', 'operators', 'SELECT')")[0][0] is True
