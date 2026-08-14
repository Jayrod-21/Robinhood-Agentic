"""Integration: the full auth loop against a REAL Postgres running the REAL migrations (001-013),
connected AS rh_auth — the grants, CHECKs, partial unique indexes, and rowcount gates in force.

The semantics under test are single-use / atomic / rowcount-gated (AUTH_THREAT_MODEL §5.4, §5.5,
§5.6, §5.8) and a mock cannot prove them. Covered revert-proofs:

  * §5.2 — unknown-email and wrong-password answers are byte-identical, and the miss path still
    performs an Argon2 verification (structural).
  * §5.4 — TOTP replay refused via the last_used_step high-water mark; the skew window cannot
    walk backwards.
  * §5.8 — five post-password failures lock (423 + retry_after); bad PASSWORDS never lock; the
    lock is per-operator; success resets the counters.
  * §5.5 — recovery codes are single-use via the rowcount-gated UPDATE, share the lockout
    counter, and using one revokes every other session.
  * §5.3 — sessions expire (absolute + idle), logout revokes server-side, a planted cookie value
    never validates, the challenge token confers nothing.
  * §5.6 — verification tokens: consumed once, address-bound, TTL'd; resend answers one identical
    202 for every state and the cooldown+insert are atomic.
  * §5.12 — auth_events rows for successes and failures alike.

Never touches the live rh-db; the container dies with the module (pattern: test_outcomes_db.py).
"""

from __future__ import annotations

import base64
import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "db"))  # migrate.py is a top-level script in db/


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _testcontainers_available() -> bool:
    """Docker and the testcontainers PACKAGE are separate preconditions: CI's backend job has a
    Docker daemon but installs only backend/requirements.txt, which does not include
    testcontainers. Guard both, or collection dies before any skip applies."""
    return importlib.util.find_spec("testcontainers") is not None


_INTEGRATION_READY = _docker_available() and _testcontainers_available()

pytestmark = pytest.mark.skipif(
    not _INTEGRATION_READY,
    reason="integration test needs docker AND the testcontainers package",
)

if _INTEGRATION_READY:  # imports guarded so collection succeeds without either precondition
    import psycopg
    import pyotp
    from fastapi.testclient import TestClient

    try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        from testcontainers.postgres import PostgresContainer

    from app.db import close_pool, reset_db_settings
    from app.main import create_app
    from app.services import auth as auth_service
    from app.services.crypto import (
        PASSWORD_HASHER,
        digest_token,
        encrypt_totp_secret,
        generate_token,
    )

PG_IMAGE = "postgres:16-alpine"  # same major the live stack pins by digest
AUTH_PASSWORD = "test-rh-auth-pw"
JSON = {"Content-Type": "application/json"}

OP1_EMAIL = "jared.op@example.com"
OP2_EMAIL = "joe.op@example.com"
OP1_PASSWORD = "correct horse battery staple 44"
OP2_PASSWORD = "another perfectly long password 9"
# Crockford base32, uppercase, exactly as bin/manage_operator.py prints them.
RECOVERY_CODES = ["ABCDEFGH01", "JKMNPQRSTV", "WXYZ012345"]

# Mid-step so a step boundary cannot roll between code generation and verification.
FIXED_NOW = 63_333_333 * 30 + 15
CURRENT_STEP = FIXED_NOW // 30


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture(scope="module")
def enc_key() -> Iterator[bytes]:
    """TOTP_SECRET_ENC_KEY for both the seeding here and the service's decrypt path."""
    key = secrets.token_bytes(32)
    old = os.environ.get("TOTP_SECRET_ENC_KEY")
    os.environ["TOTP_SECRET_ENC_KEY"] = base64.b64encode(key).decode("ascii")
    yield key
    if old is None:
        os.environ.pop("TOTP_SECRET_ENC_KEY", None)
    else:
        os.environ["TOTP_SECRET_ENC_KEY"] = old


@pytest.fixture(scope="module")
def urls(pg_container: PostgresContainer) -> Iterator[dict[str, str]]:
    """One migrated database for the module: admin URL (superuser) + auth URL (rh_auth)."""
    from migrate import EXIT_OK
    from migrate import main as migrate_main

    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    admin_root = f"postgresql://{pg_container.username}:{pg_container.password}@{host}:{port}"
    name = f"tdb_{uuid.uuid4().hex[:12]}"

    with psycopg.connect(f"{admin_root}/{pg_container.dbname}", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    admin_url = f"{admin_root}/{name}"

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = admin_url
    try:
        assert migrate_main(["up"]) == EXIT_OK
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old

    # 012 ships rh_auth with LOGIN and no password; give it one so the auth pool can authenticate.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE rh_auth WITH PASSWORD '{AUTH_PASSWORD}'")

    yield {
        "admin": admin_url,
        "auth": f"postgresql://rh_auth:{AUTH_PASSWORD}@{host}:{port}/{name}",
    }


@pytest.fixture(scope="module")
def seeded(urls, enc_key) -> dict:
    """Two operators (per-operator lockout needs two), confirmed TOTP, recovery codes — seeded as
    the DDL role, exactly the split the CLI uses (rh_auth cannot mint accounts)."""
    hasher = PASSWORD_HASHER  # THE shared hasher — the same instance the CLI stores with (§5.2)
    secret1, secret2 = pyotp.random_base32(), pyotp.random_base32()
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        op1 = conn.execute(
            "INSERT INTO operators (email, password_hash) VALUES (%s, %s) RETURNING id",
            (OP1_EMAIL, hasher.hash(OP1_PASSWORD)),
        ).fetchone()[0]
        op2 = conn.execute(
            "INSERT INTO operators (email, password_hash) VALUES (%s, %s) RETURNING id",
            (OP2_EMAIL, hasher.hash(OP2_PASSWORD)),
        ).fetchone()[0]
        for op, secret in ((op1, secret1), (op2, secret2)):
            conn.execute(
                "INSERT INTO operator_totp (operator_id, secret_encrypted, confirmed_at) "
                "VALUES (%s, %s, now())",
                (op, encrypt_totp_secret(secret, key=enc_key)),
            )
        for code in RECOVERY_CODES:
            conn.execute(
                "INSERT INTO recovery_codes (operator_id, code_hash) VALUES (%s, %s)",
                (op1, digest_token(code)),
            )
    return {"op1": op1, "op2": op2, "secret1": secret1, "secret2": secret2}


@pytest.fixture(autouse=True)
def app_env(urls, monkeypatch):
    """Point the app's AUTH pool at the migrated database AS rh_auth — grants in full force."""
    monkeypatch.setenv("AUTH_DATABASE_URL", urls["auth"])
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


@pytest.fixture(autouse=True)
def clean_state(urls, seeded):
    """Reset all mutable auth state between tests (as the superuser — rh_auth cannot, by design)."""
    yield
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        conn.execute("DELETE FROM auth_events")
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM mfa_login_challenges")
        conn.execute("DELETE FROM email_verification_tokens")
        conn.execute("UPDATE recovery_codes SET used_at = NULL")
        conn.execute(
            "UPDATE operator_totp SET last_used_step = 0, failed_attempts = 0, locked_until = NULL"
        )
        conn.execute("UPDATE operators SET email_verified_at = NULL, disabled_at = NULL")


@pytest.fixture()
def frozen_clock(monkeypatch):
    """Pin the service's TOTP clock mid-step; codes are generated for explicit steps."""
    monkeypatch.setattr(auth_service, "_time", lambda: float(FIXED_NOW))


@pytest.fixture()
def client() -> TestClient:
    # https base_url so the Secure session cookie participates in the client's jar. No context
    # manager: lifespan would mkdir the container-only /app/data paths (same as test_outcomes_db).
    return TestClient(create_app(), base_url="https://testserver")


def _code(secret: str, step: int = CURRENT_STEP) -> str:
    return pyotp.TOTP(secret).at(step * 30)


def _wrong_code(secret: str) -> str:
    valid = {_code(secret, CURRENT_STEP + o) for o in range(-2, 3)}
    return next(c for c in ("000000", "000001", "000002") if c not in valid)


def _challenge(client, email: str = OP1_EMAIL, password: str = OP1_PASSWORD) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password}, headers=JSON)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "mfa_required"
    return body["challenge_token"]


def _submit(client, challenge: str, code: str):
    return client.post(
        "/api/auth/login/totp",
        json={"challenge_token": challenge, "code": code},
        headers=JSON,
    )


def _login(client, seeded) -> None:
    res = _submit(client, _challenge(client), _code(seeded["secret1"]))
    assert res.status_code == 204, res.text


def _event_pairs(urls) -> list[tuple[str, str]]:
    with psycopg.connect(urls["admin"]) as conn:
        return conn.execute("SELECT event_type, outcome FROM auth_events ORDER BY id").fetchall()


# --- §5.2 enumeration ------------------------------------------------------------------------


def test_unknown_and_wrong_password_responses_are_byte_identical(client, seeded, urls):
    unknown = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "x"}, headers=JSON
    )
    wrong = client.post(
        "/api/auth/login", json={"email": OP1_EMAIL, "password": "wrong password"}, headers=JSON
    )
    assert unknown.status_code == wrong.status_code == 200
    assert unknown.content == wrong.content
    assert "set-cookie" not in unknown.headers
    assert "set-cookie" not in wrong.headers
    # Both failures are on the record (§5.12) — one with no operator to point at.
    events = _event_pairs(urls)
    assert events.count(("login_password", "failure")) == 2


def test_unknown_email_still_invokes_argon2_verify(client, monkeypatch):
    """Structural half of the timing defense: the miss path burns a real verification."""
    calls = []
    monkeypatch.setattr(auth_service, "_dummy_verify", lambda: calls.append(True))
    client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "x"}, headers=JSON
    )
    assert calls, "unknown-email branch must run the dummy Argon2 verification"


def test_correct_password_issues_no_session_and_challenge_confers_nothing(client, seeded):
    challenge = _challenge(client)
    # No cookie from the password step (§4) — the client jar stays empty.
    assert auth_service.SESSION_COOKIE_NAME not in client.cookies
    # The challenge token is not a session: presenting it as one buys nothing.
    res = client.get("/api/db/health", headers={"Cookie": f"__Host-rh_sid={challenge}"})
    assert res.status_code == 401
    res = client.get("/api/auth/me", headers={"Cookie": f"__Host-rh_sid={challenge}"})
    assert res.status_code == 401


def test_oversize_password_never_reaches_argon2(client, monkeypatch):
    real_verifies = []
    dummy_verifies = []
    real_verify = auth_service._verify_password
    monkeypatch.setattr(
        auth_service,
        "_verify_password",
        lambda stored, candidate: real_verifies.append(len(candidate))
        or real_verify(stored, candidate),
    )
    monkeypatch.setattr(auth_service, "_dummy_verify", lambda: dummy_verifies.append(True))
    res = client.post(
        "/api/auth/login", json={"email": OP1_EMAIL, "password": "x" * 300}, headers=JSON
    )
    assert res.status_code == 200
    assert res.json() == {"status": "invalid_credentials"}
    # The oversize input took the dummy-verify path; the real verifier never saw it (§5.1).
    assert dummy_verifies
    assert real_verifies == []


# --- §5.4 TOTP replay + window ---------------------------------------------------------------


def test_same_code_cannot_be_used_twice(client, seeded, urls, frozen_clock):
    code = _code(seeded["secret1"])
    assert _submit(client, _challenge(client), code).status_code == 204
    # Replay within the same step, on a fresh challenge: refused, high-water mark unchanged.
    replay = _submit(client, _challenge(client), code)
    assert replay.status_code == 401
    assert "set-cookie" not in replay.headers
    with psycopg.connect(urls["admin"]) as conn:
        hwm, failed = conn.execute(
            "SELECT last_used_step, failed_attempts FROM operator_totp WHERE operator_id = %s",
            (seeded["op1"],),
        ).fetchone()
    assert hwm == CURRENT_STEP
    # A SEQUENTIAL replay (the winner already committed) is a real failed attempt and counts a
    # §5.8 strike — unlike the concurrent race loser, which holds a still-valid code (see
    # test_totp_race_loser_rejected_without_a_strike).
    assert failed == 1


def test_earlier_step_code_rejected_after_later_one(client, seeded, frozen_clock):
    """The ±1 skew window must not let an attacker walk backwards (§5.4)."""
    assert _submit(client, _challenge(client), _code(seeded["secret1"], CURRENT_STEP + 1)).status_code == 204
    stale = _submit(client, _challenge(client), _code(seeded["secret1"], CURRENT_STEP))
    assert stale.status_code == 401


def test_window_is_exactly_one_step_end_to_end(client, seeded, frozen_clock):
    # −1 first (lowest step), then +1 — both inside the window and monotonically increasing.
    assert _submit(client, _challenge(client), _code(seeded["secret1"], CURRENT_STEP - 1)).status_code == 204
    assert _submit(client, _challenge(client), _code(seeded["secret1"], CURRENT_STEP + 1)).status_code == 204
    for offset in (-2, 2):
        res = _submit(client, _challenge(client), _code(seeded["secret1"], CURRENT_STEP + offset))
        assert res.status_code == 401, f"offset {offset} must be outside the window"


def test_challenge_is_single_use_and_purpose_scoped(client, seeded, urls, frozen_clock):
    challenge = _challenge(client)
    assert _submit(client, challenge, _code(seeded["secret1"])).status_code == 204
    # Consumed challenge cannot be replayed, even with a fresh valid code.
    res = _submit(client, challenge, _code(seeded["secret1"], CURRENT_STEP + 1))
    assert res.status_code == 401
    # An 'enroll'-purpose challenge is invisible to the login route (§5.4).
    token = generate_token()
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO mfa_login_challenges (operator_id, token_hash, purpose, expires_at) "
            "VALUES (%s, %s, 'enroll', now() + interval '5 minutes')",
            (seeded["op1"], digest_token(token)),
        )
    assert _submit(client, token, _code(seeded["secret1"], CURRENT_STEP + 1)).status_code == 401


class _RaceLoserConn:
    """Simulates the §5.4 concurrent-duplicate loser deterministically: the SELECT reads a stale
    ``last_used_step``, then the rowcount-gated UPDATE matches nothing because the concurrent
    winner advanced the mark in between. Every other statement passes through to the real
    connection, so the transaction's bookkeeping is exactly production's."""

    class _NoRows:
        rowcount = 0

    def __init__(self, real: "psycopg.Connection") -> None:
        self._real = real

    def execute(self, sql: str, params=None):
        if "SET last_used_step" in sql:
            return self._NoRows()
        return self._real.execute(sql, params)


def test_totp_race_loser_rejected_without_a_strike(seeded, urls, frozen_clock):
    """A double-click's loser holds the RIGHT code — losing the last_used_step rowcount gate must
    reject without a §5.8 strike, or five fast double-clicks lock the operator out."""
    issued = auth_service.password_login(OP1_EMAIL, OP1_PASSWORD)
    assert issued is not None
    with auth_service._auth_db() as conn:
        outcome = auth_service._complete_mfa_tx(
            _RaceLoserConn(conn), issued.token, _code(seeded["secret1"]), None, None
        )
    assert outcome.kind == "rejected"
    with psycopg.connect(urls["admin"]) as conn:
        failed = conn.execute(
            "SELECT failed_attempts FROM operator_totp WHERE operator_id = %s", (seeded["op1"],)
        ).fetchone()[0]
    assert failed == 0, "losing the race on a VALID code must not count as a failed attempt"
    # The distinct outcome is on the audit record — not a generic failure.
    assert ("mfa_totp", "replayed") in _event_pairs(urls)


# --- §5.8 lockout ----------------------------------------------------------------------------


def test_lockout_after_fifth_failure_returns_423_with_retry_after(client, seeded, urls, frozen_clock):
    bad = _wrong_code(seeded["secret1"])
    challenge = _challenge(client)
    for _ in range(4):
        assert _submit(client, challenge, bad).status_code == 401
    fifth = _submit(client, challenge, bad)
    assert fifth.status_code == 423
    body = fifth.json()
    assert 0 < body["retry_after"] <= 15 * 60
    assert body["reason"]
    # Locked means locked: the CORRECT code is refused with the remaining wait (checked before
    # any verification work), and the lock landed in the audit log.
    good = _submit(client, _challenge(client), _code(seeded["secret1"]))
    assert good.status_code == 423
    assert ("totp_lockout", "locked") in _event_pairs(urls)


def test_lockout_requires_correct_password_first(client, seeded, urls):
    """N bad PASSWORDS never touch the lockout counter — the drive-by DoS does not exist."""
    for _ in range(7):
        res = client.post(
            "/api/auth/login", json={"email": OP2_EMAIL, "password": "not it"}, headers=JSON
        )
        assert res.status_code == 200
        assert res.json() == {"status": "invalid_credentials"}
    with psycopg.connect(urls["admin"]) as conn:
        failed, locked = conn.execute(
            "SELECT failed_attempts, locked_until FROM operator_totp WHERE operator_id = %s",
            (seeded["op2"],),
        ).fetchone()
    assert failed == 0
    assert locked is None


def test_lockout_is_per_operator(client, seeded, frozen_clock):
    bad = _wrong_code(seeded["secret1"])
    challenge = _challenge(client)
    for _ in range(5):
        _submit(client, challenge, bad)
    # op1 is locked; op2 authenticates normally — the blast radius is one operator (§5.8).
    res = _submit(
        client, _challenge(client, OP2_EMAIL, OP2_PASSWORD), _code(seeded["secret2"])
    )
    assert res.status_code == 204


def test_successful_auth_resets_counters(client, seeded, urls, frozen_clock):
    challenge = _challenge(client)
    for _ in range(3):
        _submit(client, challenge, _wrong_code(seeded["secret1"]))
    assert _submit(client, challenge, _code(seeded["secret1"])).status_code == 204
    with psycopg.connect(urls["admin"]) as conn:
        failed, locked = conn.execute(
            "SELECT failed_attempts, locked_until FROM operator_totp WHERE operator_id = %s",
            (seeded["op1"],),
        ).fetchone()
    assert failed == 0
    assert locked is None


# --- §5.5 recovery codes ---------------------------------------------------------------------


def test_recovery_code_single_use(client, seeded, urls, frozen_clock):
    code = RECOVERY_CODES[0]
    assert _submit(client, _challenge(client), code).status_code == 204
    with psycopg.connect(urls["admin"]) as conn:
        used_at = conn.execute(
            "SELECT used_at FROM recovery_codes WHERE code_hash = %s", (digest_token(code),)
        ).fetchone()[0]
    assert used_at is not None
    # Replay: the rowcount-gated UPDATE matches nothing, the attempt fails and COUNTS (§5.5:
    # the recovery path shares the lockout counter with TOTP).
    replay = _submit(client, _challenge(client), code)
    assert replay.status_code == 401
    with psycopg.connect(urls["admin"]) as conn:
        failed = conn.execute(
            "SELECT failed_attempts FROM operator_totp WHERE operator_id = %s", (seeded["op1"],)
        ).fetchone()[0]
    assert failed == 1
    events = _event_pairs(urls)
    assert ("mfa_recovery", "success") in events
    assert ("mfa_recovery", "failure") in events


def test_recovery_code_case_and_separator_tolerant(client, seeded, urls, frozen_clock):
    """Crockford base32 is canonically case-insensitive, and grouping separators carry no
    entropy — a correctly transcribed code succeeds regardless of case or grouping, and the
    variants never cost a §5.8 strike (five presentation typos must not lock the operator out
    of a live brokerage view)."""
    assert _submit(client, _challenge(client), RECOVERY_CODES[0].lower()).status_code == 204
    hyphenated = f"{RECOVERY_CODES[1][:5]}-{RECOVERY_CODES[1][5:]}".lower()
    assert _submit(client, _challenge(client), hyphenated).status_code == 204
    spaced = f" {RECOVERY_CODES[2][:5]} {RECOVERY_CODES[2][5:]} "
    assert _submit(client, _challenge(client), spaced).status_code == 204
    with psycopg.connect(urls["admin"]) as conn:
        failed = conn.execute(
            "SELECT failed_attempts FROM operator_totp WHERE operator_id = %s", (seeded["op1"],)
        ).fetchone()[0]
        used = conn.execute(
            "SELECT count(*) FROM recovery_codes WHERE used_at IS NOT NULL"
        ).fetchone()[0]
    assert failed == 0
    assert used == 3  # each variant consumed its own canonical code exactly once


def test_totp_code_tolerates_the_space_authenticators_display(client, seeded, urls, frozen_clock):
    """The same tolerance, for the OTHER code type that enters this field.

    Google Authenticator, Authy and Microsoft Authenticator all render the code as "123 456", and
    operators type what they are shown. Before the fold, that correct code matched neither shape,
    fell through to the wrong-guess path, and was scored as a §5.8 failure — identical in outcome
    and in the lockout counter to an attacker's miss. Five of them locked the operator out on
    input that was right every time.
    """
    code = _code(seeded["secret1"])
    spaced = f"{code[:3]} {code[3:]}"
    assert _submit(client, _challenge(client), spaced).status_code == 204
    with psycopg.connect(urls["admin"]) as conn:
        failed = conn.execute(
            "SELECT failed_attempts FROM operator_totp WHERE operator_id = %s", (seeded["op1"],)
        ).fetchone()[0]
    assert failed == 0, "the authenticator's own grouping space must never cost a lockout strike"


class _ChallengeRaceConn:
    """Simulates the §5.5 concurrent double-submit loser deterministically: the top-of-step
    SELECT sees the challenge as unconsumed, then every rowcount-gated stamp of consumed_at
    matches nothing because the concurrent winner consumed it in between. Everything else
    passes through, so the loser's bookkeeping is exactly production's — including the COMMIT
    a domain rejection gets under this module's transaction shape."""

    class _NoRows:
        rowcount = 0

    def __init__(self, real: "psycopg.Connection") -> None:
        self._real = real

    def execute(self, sql: str, params=None):
        if "mfa_login_challenges SET consumed_at" in sql:
            return self._NoRows()
        return self._real.execute(sql, params)


def test_recovery_loser_of_challenge_race_burns_nothing(client, seeded, urls, frozen_clock):
    """§5.5 ordering: the challenge must be consumed FIRST — it is the single-use serialisation
    point for the whole step. Two concurrent submissions carrying two different valid recovery
    codes race on it; the loser must not burn its code, must not revoke a single session, and
    must not take a §5.8 strike. Before the reorder the loser burned its code and revoked every
    session and only THEN lost the (end-of-step) challenge gate — with the damage committed."""
    _login(client, seeded)  # a live session that must survive the loser's attempt
    issued = auth_service.password_login(OP1_EMAIL, OP1_PASSWORD)
    assert issued is not None
    with auth_service._auth_db() as conn:
        outcome = auth_service._complete_mfa_tx(
            _ChallengeRaceConn(conn), issued.token, RECOVERY_CODES[0], None, None
        )
    assert outcome.kind == "rejected"
    with psycopg.connect(urls["admin"]) as conn:
        used_at = conn.execute(
            "SELECT used_at FROM recovery_codes WHERE code_hash = %s",
            (digest_token(RECOVERY_CODES[0]),),
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT failed_attempts FROM operator_totp WHERE operator_id = %s", (seeded["op1"],)
        ).fetchone()[0]
    assert used_at is None, "the loser's recovery code must not be burnt"
    assert failed == 0, "losing the challenge race is not a wrong code"
    assert client.get("/api/auth/me").status_code == 200, "existing sessions must survive"


def test_recovery_code_use_revokes_every_other_session(client, seeded, urls, frozen_clock):
    # Session A via TOTP.
    _login(client, seeded)
    cookie_a = client.cookies.get(auth_service.SESSION_COOKIE_NAME)
    assert client.get("/api/auth/me").status_code == 200
    # Session B via a recovery code — "I lost my authenticator" ⇒ other sessions are revoked.
    res = _submit(client, _challenge(client), RECOVERY_CODES[1])
    assert res.status_code == 204
    assert client.get("/api/auth/me").status_code == 200  # the new session works
    replayed = client.get("/api/auth/me", headers={"Cookie": f"__Host-rh_sid={cookie_a}"})
    assert replayed.status_code == 401  # the old one is gone, server-side


# --- §5.3 sessions ---------------------------------------------------------------------------


def test_me_reports_operator_and_dependency_admits_the_session(client, seeded, frozen_clock):
    _login(client, seeded)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"email": OP1_EMAIL, "email_verified": False}
    # The app-wide gate admits the same session on an ordinary router.
    assert client.get("/api/db/health").status_code == 200


def test_expired_and_idle_sessions_rejected(client, seeded, urls, frozen_clock):
    _login(client, seeded)
    assert client.get("/api/auth/me").status_code == 200
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        # ck_sessions_expiry requires expires_at > created_at, so age the whole row.
        conn.execute(
            "UPDATE sessions SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 second'"
        )
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/db/health").status_code == 401
    # Fresh session (next step's code — the §5.4 high-water mark refuses a same-step reuse);
    # kill it via the idle timeout instead (clock injected into the DB, not slept).
    res = _submit(client, _challenge(client), _code(seeded["secret1"], CURRENT_STEP + 1))
    assert res.status_code == 204, res.text
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        conn.execute(
            "UPDATE sessions SET last_seen_at = now() - interval '25 hours' "
            "WHERE revoked_at IS NULL"
        )
    assert client.get("/api/auth/me").status_code == 401


def test_idle_window_not_renewed_by_polling_gets(client, seeded, urls, frozen_clock):
    """§5.3: the dashboard re-GETs its data endpoints on SWR timers (10-30 s), so a safe-method
    request through the app-wide gate must VALIDATE the session without renewing the idle
    window — otherwise an unattended open tab keeps the session alive for the full 14-day
    absolute expiry and the idle timeout never fires. Operator presence still renews: the shell
    revalidates /api/auth/me on real window-focus events, and that path touches."""
    _login(client, seeded)
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = now() - interval '2 hours'")
    assert client.get("/api/db/health").status_code == 200  # admitted through the gate...
    with psycopg.connect(urls["admin"]) as conn:
        renewed = conn.execute(
            "SELECT last_seen_at > now() - interval '1 hour' FROM sessions"
        ).fetchone()[0]
    assert renewed is False, "a polling GET must not renew the idle window"
    # The focus-driven /me revalidation is the human-presence signal — it renews.
    assert client.get("/api/auth/me").status_code == 200
    with psycopg.connect(urls["admin"]) as conn:
        renewed = conn.execute(
            "SELECT last_seen_at > now() - interval '1 hour' FROM sessions"
        ).fetchone()[0]
    assert renewed is True, "/api/auth/me (window-focus revalidation) must renew it"


def test_logout_revokes_server_side_not_just_cookie(client, seeded, urls, frozen_clock):
    _login(client, seeded)
    cookie = client.cookies.get(auth_service.SESSION_COOKIE_NAME)
    assert client.post("/api/auth/logout", json={}, headers=JSON).status_code == 204
    # Replaying the pre-logout value is 401: the row is stamped revoked_at, the jar is irrelevant.
    res = client.get("/api/auth/me", headers={"Cookie": f"__Host-rh_sid={cookie}"})
    assert res.status_code == 401
    with psycopg.connect(urls["admin"]) as conn:
        revoked = conn.execute(
            "SELECT revoked_at FROM sessions WHERE token_hash = %s", (digest_token(cookie),)
        ).fetchone()[0]
    assert revoked is not None


def test_presented_unknown_cookie_is_never_adopted(client, seeded, frozen_clock):
    """Fixation, structurally (§5.3): a planted value does not exist in sessions and never
    validates; completing a login issues a DIFFERENT value."""
    planted = "f" * 43
    res = client.get("/api/auth/me", headers={"Cookie": f"__Host-rh_sid={planted}"})
    assert res.status_code == 401
    _login(client, seeded)
    issued = client.cookies.get(auth_service.SESSION_COOKIE_NAME)
    assert issued != planted
    res = client.get("/api/auth/me", headers={"Cookie": f"__Host-rh_sid={planted}"})
    assert res.status_code == 401


def test_session_cookie_attributes_from_a_real_login(client, seeded, frozen_clock):
    res = _submit(client, _challenge(client), _code(seeded["secret1"]))
    assert res.status_code == 204
    header = res.headers["set-cookie"]
    low = header.lower()
    assert header.startswith("__Host-rh_sid=")
    assert "httponly" in low and "secure" in low and "samesite=strict" in low
    assert "path=/" in low and "domain" not in low


# --- §5.6 email verification -----------------------------------------------------------------


def test_verification_token_consumed_once_and_confers_no_session(client, seeded, urls):
    decision = auth_service.request_verification_resend(OP1_EMAIL)
    assert decision.send and decision.token
    res = client.post("/api/auth/verify", json={"token": decision.token}, headers=JSON)
    assert res.status_code == 200
    assert res.json() == {"status": "verified"}
    assert "set-cookie" not in res.headers  # redemption stamps email_verified_at, nothing else
    with psycopg.connect(urls["admin"]) as conn:
        verified = conn.execute(
            "SELECT email_verified_at FROM operators WHERE id = %s", (seeded["op1"],)
        ).fetchone()[0]
        session_count = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    assert verified is not None
    assert session_count == 0
    # Second redemption resolves to the friendly already_verified (§5.6).
    res = client.post("/api/auth/verify", json={"token": decision.token}, headers=JSON)
    assert res.status_code == 200
    assert res.json() == {"status": "already_verified"}
    # And /me reflects the stamp.
    c2 = TestClient(create_app(), base_url="https://testserver")
    challenge = _challenge(c2)
    code = pyotp.TOTP(seeded["secret1"]).now()
    if _submit(c2, challenge, code).status_code == 204:
        assert c2.get("/api/auth/me").json()["email_verified"] is True


def test_expired_and_address_bound_tokens_rejected(client, seeded, urls):
    expired = generate_token()
    stale = generate_token()
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO email_verification_tokens "
            "(operator_id, token_hash, email, created_at, expires_at) "
            "VALUES (%s, %s, %s, now() - interval '2 days', now() - interval '1 day')",
            (seeded["op1"], digest_token(expired), OP1_EMAIL),
        )
        conn.execute(
            "INSERT INTO email_verification_tokens (operator_id, token_hash, email, expires_at) "
            "VALUES (%s, %s, %s, now() + interval '1 day')",
            (seeded["op2"], digest_token(stale), "previous.address@example.com"),
        )
    res = client.post("/api/auth/verify", json={"token": expired}, headers=JSON)
    assert res.status_code == 400
    assert "expire" in res.json()["detail"]
    # §5.6 address binding: a token for an address the operator no longer has cannot verify.
    res = client.post("/api/auth/verify", json={"token": stale}, headers=JSON)
    assert res.status_code == 400
    with psycopg.connect(urls["admin"]) as conn:
        verified = conn.execute(
            "SELECT email_verified_at FROM operators WHERE id = %s", (seeded["op2"],)
        ).fetchone()[0]
    assert verified is None


def test_resend_response_identical_across_states_and_cooldown_atomic(client, seeded, urls):
    known = client.post("/api/auth/verify/resend", json={"email": OP1_EMAIL}, headers=JSON)
    unknown = client.post(
        "/api/auth/verify/resend", json={"email": "nobody@example.com"}, headers=JSON
    )
    # Immediate second resend for the known address: suppressed by the cooldown — invisibly.
    cooled = client.post("/api/auth/verify/resend", json={"email": OP1_EMAIL}, headers=JSON)
    assert known.status_code == unknown.status_code == cooled.status_code == 202
    assert known.content == unknown.content == cooled.content
    with psycopg.connect(urls["admin"]) as conn:
        total, live = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE consumed_at IS NULL "
            "AND invalidated_at IS NULL) FROM email_verification_tokens"
        ).fetchone()
    # One token minted, one live — the cooldown probe and insert are atomic (§5.6), and the
    # partial unique index uq_evt_one_live_per_operator backs it structurally.
    assert total == 1
    assert live == 1


# --- §5.12 audit -----------------------------------------------------------------------------


def test_every_auth_outcome_writes_an_event(client, seeded, urls, frozen_clock):
    client.post("/api/auth/login", json={"email": "no@example.com", "password": "x"}, headers=JSON)
    client.post("/api/auth/login", json={"email": OP1_EMAIL, "password": "wrong"}, headers=JSON)
    challenge = _challenge(client)
    _submit(client, challenge, _wrong_code(seeded["secret1"]))
    _submit(client, challenge, _code(seeded["secret1"]))
    client.post("/api/auth/logout", json={}, headers=JSON)
    events = set(_event_pairs(urls))
    assert {
        ("login_password", "failure"),
        ("login_password", "success"),
        ("mfa_totp", "failure"),
        ("mfa_totp", "success"),
        ("logout", "success"),
    } <= events
