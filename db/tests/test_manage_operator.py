"""bin/manage_operator.py against a real throwaway Postgres migrated 001-012.

The CLI is the ONLY account-lifecycle surface (AUTH_THREAT_MODEL §4, §5.7 — there is deliberately
no signup route and no self-service reset), so these tests are the only automated proof that a
seeded operator is actually usable: the password verifies as Argon2id, the stored TOTP secret is
ciphertext that round-trips through THE shared implementation in backend/app/services/crypto.py
(a format divergence between CLI and backend produces operators who cannot log in, with nothing
visibly wrong at either end — the round-trip test here is what catches it), a recovery code
consumes exactly once, and unlock/disable/reset actually change the rows they claim to.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

# conftest puts db/ on sys.path (for migrate); the CLI lives in bin/ and itself puts backend/ on
# sys.path (for app.services.crypto), mirroring how the runner container reaches both.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))

import manage_operator as mo
from app.services.crypto import decrypt_totp_secret, load_enc_key
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from migrate import EXIT_OK
from migrate import main as migrate_main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PG_IMAGE = "postgres:16-alpine"

PASSWORD = "orbit-walnut-49-chandelier"
PASSWORD2 = "granite-otter-77-lighthouse"

URI_RE = re.compile(r"otpauth://\S+")
CODE_LINE_RE = re.compile(r"^\s*\d+\.\s+([0-9A-Z]{10})$", re.MULTILINE)


@pytest.fixture(scope="session")
def op_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


def _admin_url(pg: PostgresContainer) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


@pytest.fixture
def db(op_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, fully-migrated database per test. DATABASE_URL points the CLI at it, and a valid
    32-byte TOTP_SECRET_ENC_KEY is in the environment — the same contract the wrapper provides."""
    name = f"odb_{uuid.uuid4().hex[:12]}"
    admin = _admin_url(op_pg)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TOTP_SECRET_ENC_KEY", base64.b64encode(os.urandom(32)).decode())
    assert migrate_main(["up", "--migrations-dir", str(REPO_MIGRATIONS)]) == EXIT_OK
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def q(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def run_cli(
    argv: list[str],
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    stdin: str | None = None,
) -> tuple[int, str]:
    """Invoke the CLI in-process, exactly as `python bin/manage_operator.py …` would run it."""
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = mo.main(argv)
    return code, capsys.readouterr().out


def seed(
    email: str,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    password: str = PASSWORD,
) -> tuple[int, str]:
    return run_cli(
        ["seed", "--email", email, "--password-stdin"], capsys, monkeypatch, stdin=password + "\n"
    )


def uri_secret(out: str) -> str:
    """The base32 secret advertised by the printed provisioning URI — what the operator's
    authenticator will actually hold, so it is the reference value for every ciphertext check."""
    uri = URI_RE.search(out)
    assert uri, f"no otpauth:// URI in output:\n{out}"
    secret = parse_qs(urlparse(uri.group()).query)["secret"][0]
    assert re.fullmatch(r"[A-Z2-7]{32}", secret), "URI secret is not 32-char base32"
    return secret


def insert_session(db: str, operator_id: int) -> int:
    """A live session row, admin-inserted with shape-valid values (64-hex token digest)."""
    token_hash = hashlib.sha256(os.urandom(32)).hexdigest()
    return q(
        db,
        "INSERT INTO sessions (operator_id, token_hash, expires_at)"
        " VALUES (%s, %s, now() + interval '1 day') RETURNING id",
        (operator_id, token_hash),
    )[0][0]


def events(db: str, event_type: str) -> int:
    return q(db, "SELECT count(*) FROM auth_events WHERE event_type = %s", (event_type,))[0][0]


# ── seed ─────────────────────────────────────────────────────────────────────────────────────


def test_seed_password_is_argon2id_and_verifies(db, capsys, monkeypatch) -> None:
    code, _ = seed("jared@example.com", capsys, monkeypatch)
    assert code == mo.EXIT_OK
    (stored,) = q(db, "SELECT password_hash FROM operators WHERE email = 'jared@example.com'")[0]
    # The PHC prefix is what 012's CHECK pins; verify() is the proof the hash is USABLE — a hash
    # that satisfies the CHECK but was made from the wrong input would pass the first assertion
    # and fail the second.
    assert stored.startswith("$argon2id$")
    assert PasswordHasher().verify(stored, PASSWORD)
    with pytest.raises(VerifyMismatchError):
        PasswordHasher().verify(stored, PASSWORD + "x")
    assert events(db, "operator_seeded") == 1


def test_hash_uses_argon2id_with_pinned_params(db, capsys, monkeypatch) -> None:
    """§5.2 timing: the CLI hashes with THE shared pinned hasher (crypto.PASSWORD_HASHER —
    m=65536, t=3, p=1), never a bare PasswordHasher() (default p=4). PHC strings are
    self-describing, so a default-params hash still VERIFIES — but every real login then runs
    Argon2 at a different cost than the backend's pinned dummy verify, and login latency becomes
    an email-existence oracle (measured 2.5x on this host before the parameters were unified)."""
    pinned_prefix = "$argon2id$v=19$m=65536,t=3,p=1$"
    assert seed("jared@example.com", capsys, monkeypatch)[0] == mo.EXIT_OK
    (stored,) = q(db, "SELECT password_hash FROM operators")[0]
    assert stored.startswith(pinned_prefix), f"seed drifted off the pinned params: {stored[:34]}"
    # reset-password must stay on the same hasher — it writes the same column.
    code, _ = run_cli(
        ["reset-password", "--email", "jared@example.com", "--password-stdin"],
        capsys, monkeypatch, stdin=PASSWORD2 + "\n",
    )
    assert code == mo.EXIT_OK
    (rotated,) = q(db, "SELECT password_hash FROM operators")[0]
    assert rotated.startswith(pinned_prefix), f"reset drifted off the pinned params: {rotated[:34]}"


def test_seed_stores_ciphertext_that_roundtrips_through_the_shared_module(db, capsys, monkeypatch) -> None:
    code, out = seed("jared@example.com", capsys, monkeypatch)
    assert code == mo.EXIT_OK
    secret = uri_secret(out)
    (stored,) = q(db, "SELECT secret_encrypted FROM operator_totp")[0]
    # Not the plaintext, not containing the plaintext — the DB CHECK's length floor makes a bare
    # base32 secret unstorable, but a prefixed/suffixed plaintext would slip past it.
    assert stored != secret
    assert secret not in stored
    # THE round-trip: decrypting with the real backend module recovers exactly what the
    # provisioning URI advertised. This is the test that catches a CLI/backend format divergence.
    assert decrypt_totp_secret(stored, load_enc_key()) == secret
    # Confirmed at creation: the printed URI IS the enrolment (possession proven out-of-band on
    # the host), and no in-app confirm route exists — login reads only confirmed enrolments
    # (§5.4), so a NULL here would strand every seeded operator at the second factor.
    assert q(db, "SELECT confirmed_at FROM operator_totp")[0][0] is not None


def test_seed_recovery_codes_shown_once_hashed_at_rest_single_use(db, capsys, monkeypatch) -> None:
    _, out = seed("jared@example.com", capsys, monkeypatch)
    codes = CODE_LINE_RE.findall(out)
    assert len(codes) == 10 and len(set(codes)) == 10
    crockford = set(mo.CROCKFORD_ALPHABET)
    assert all(len(c) == 10 and set(c) <= crockford for c in codes)
    assert not any(ch in "ILOU" for c in codes for ch in c)

    stored = {r[0] for r in q(db, "SELECT code_hash FROM recovery_codes")}
    assert stored == {hashlib.sha256(c.encode()).hexdigest() for c in codes}
    # Plaintext never at rest: no stored value equals (or contains) a printed code.
    assert all(c not in h for c in codes for h in stored)

    # Single-use, enforced the way the backend will consume them (§5.5): the rowcount-gated
    # UPDATE succeeds exactly once for the same code.
    target = hashlib.sha256(codes[0].encode()).hexdigest()
    consume = (
        "UPDATE recovery_codes SET used_at = now()"
        " WHERE code_hash = %s AND used_at IS NULL"
    )
    with psycopg.connect(db, autocommit=True) as conn:
        assert conn.execute(consume, (target,)).rowcount == 1  # verifies once
        assert conn.execute(consume, (target,)).rowcount == 0  # and never twice


def test_seed_never_prints_password_hash_or_key(db, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TOTP_SECRET_ENC_KEY", key := base64.b64encode(os.urandom(32)).decode())
    _, out = seed("jared@example.com", capsys, monkeypatch)
    assert PASSWORD not in out
    assert key not in out
    assert "$argon2id$" not in out


def test_seed_duplicate_email_rejected_case_insensitively(db, capsys, monkeypatch) -> None:
    assert seed("jared@example.com", capsys, monkeypatch)[0] == mo.EXIT_OK
    # uq_operators_email is on lower(email): a case-variant is the SAME operator and must be
    # refused as validation (1), not surface as a SQL failure (2).
    assert seed("JARED@Example.COM", capsys, monkeypatch)[0] == mo.EXIT_VALIDATION
    assert q(db, "SELECT count(*) FROM operators")[0][0] == 1
    assert events(db, "operator_seeded") == 1


@pytest.mark.parametrize(
    "bad",
    [
        "short12345",  # 10 chars — under the 12-char floor
        "password12345",  # on the common-password block list
        "CORRECTHORSEBATTERYSTAPLE",  # block list is case-insensitive
        "x" * 300,  # over the 256-byte anti-DoS cap (§5.1)
    ],
)
def test_weak_password_rejected(db, capsys, monkeypatch, bad) -> None:
    code, _ = seed("jared@example.com", capsys, monkeypatch, password=bad)
    assert code == mo.EXIT_VALIDATION
    assert q(db, "SELECT count(*) FROM operators")[0][0] == 0


def test_bad_encryption_key_fails_validation_before_any_write(db, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TOTP_SECRET_ENC_KEY", base64.b64encode(b"way-too-short").decode())
    code, _out = run_cli(
        ["seed", "--email", "jared@example.com", "--password-stdin"],
        capsys, monkeypatch, stdin=PASSWORD + "\n",
    )
    assert code == mo.EXIT_VALIDATION
    assert q(db, "SELECT count(*) FROM operators")[0][0] == 0


# ── unlock ───────────────────────────────────────────────────────────────────────────────────


def test_unlock_clears_lockout_state(db, capsys, monkeypatch) -> None:
    seed("jared@example.com", capsys, monkeypatch)
    q(db, "UPDATE operator_totp SET failed_attempts = 5, locked_until = now() + interval '15 min'")
    assert q(db, "SELECT locked_until FROM operator_totp")[0][0] is not None  # the lock is real

    code, _ = run_cli(["unlock", "--email", "Jared@Example.com"], capsys, monkeypatch)
    assert code == mo.EXIT_OK
    locked_until, failed = q(db, "SELECT locked_until, failed_attempts FROM operator_totp")[0]
    assert locked_until is None and failed == 0
    assert events(db, "totp_unlocked") == 1


def test_unlock_unknown_operator_is_validation_error(db, capsys, monkeypatch) -> None:
    assert run_cli(["unlock", "--email", "nobody@example.com"], capsys, monkeypatch)[0] == mo.EXIT_VALIDATION


# ── disable ──────────────────────────────────────────────────────────────────────────────────


def test_disable_tombstones_and_revokes_sessions(db, capsys, monkeypatch) -> None:
    seed("jared@example.com", capsys, monkeypatch)
    (op_id,) = q(db, "SELECT id FROM operators")[0]
    sid = insert_session(db, op_id)

    code, _ = run_cli(["disable", "--email", "jared@example.com"], capsys, monkeypatch)
    assert code == mo.EXIT_OK
    assert q(db, "SELECT disabled_at FROM operators WHERE id = %s", (op_id,))[0][0] is not None
    # Revoked by stamp, never deleted (§11.5) — the row survives as history.
    assert q(db, "SELECT revoked_at FROM sessions WHERE id = %s", (sid,))[0][0] is not None
    assert events(db, "operator_disabled") == 1

    # Idempotent: a second disable is a no-op success, and does not double-log the event.
    assert run_cli(["disable", "--email", "jared@example.com"], capsys, monkeypatch)[0] == mo.EXIT_OK
    assert events(db, "operator_disabled") == 1


# ── reset-password ───────────────────────────────────────────────────────────────────────────


def test_reset_password_rotates_hash_and_revokes_sessions(db, capsys, monkeypatch) -> None:
    seed("jared@example.com", capsys, monkeypatch)
    (op_id,) = q(db, "SELECT id FROM operators")[0]
    sid = insert_session(db, op_id)
    (old_hash,) = q(db, "SELECT password_hash FROM operators")[0]

    code, _ = run_cli(
        ["reset-password", "--email", "jared@example.com", "--password-stdin"],
        capsys, monkeypatch, stdin=PASSWORD2 + "\n",
    )
    assert code == mo.EXIT_OK
    (new_hash,) = q(db, "SELECT password_hash FROM operators")[0]
    assert new_hash != old_hash
    assert PasswordHasher().verify(new_hash, PASSWORD2)
    with pytest.raises(VerifyMismatchError):  # the old password is dead
        PasswordHasher().verify(new_hash, PASSWORD)
    assert q(db, "SELECT revoked_at FROM sessions WHERE id = %s", (sid,))[0][0] is not None
    assert events(db, "password_reset") == 1


# ── reset-totp ───────────────────────────────────────────────────────────────────────────────


def test_reset_totp_replaces_secret_codes_and_state(db, capsys, monkeypatch) -> None:
    _, seed_out = seed("jared@example.com", capsys, monkeypatch)
    old_secret = uri_secret(seed_out)
    old_hashes = {r[0] for r in q(db, "SELECT code_hash FROM recovery_codes")}
    (op_id,) = q(db, "SELECT id FROM operators")[0]
    sid = insert_session(db, op_id)
    # Simulate a confirmed, used, locked enrolment — reset must clear ALL of it.
    q(db, "UPDATE operator_totp SET confirmed_at = now(), last_used_step = 12345,"
          " failed_attempts = 3, locked_until = now() + interval '15 min'")

    code, out = run_cli(["reset-totp", "--email", "jared@example.com"], capsys, monkeypatch)
    assert code == mo.EXIT_OK

    new_secret = uri_secret(out)
    assert new_secret != old_secret
    stored, confirmed, step, failed, locked = q(
        db,
        "SELECT secret_encrypted, confirmed_at, last_used_step, failed_attempts, locked_until"
        " FROM operator_totp",
    )[0]
    # The new ciphertext decrypts to the NEW secret, every piece of state tied to the old one is
    # gone, and the new enrolment is confirmed immediately (the printed URI is the possession
    # proof — same reasoning as seed; a NULL would strand the account after every reset).
    assert decrypt_totp_secret(stored, load_enc_key()) == new_secret
    assert confirmed is not None and step == 0 and failed == 0 and locked is None

    # §5.5: codes issued alongside the old secret must not survive its replacement.
    new_hashes = {r[0] for r in q(db, "SELECT code_hash FROM recovery_codes")}
    assert len(new_hashes) == 10
    assert new_hashes.isdisjoint(old_hashes)
    new_codes = CODE_LINE_RE.findall(out)
    assert {hashlib.sha256(c.encode()).hexdigest() for c in new_codes} == new_hashes

    assert q(db, "SELECT revoked_at FROM sessions WHERE id = %s", (sid,))[0][0] is not None
    assert events(db, "totp_reset") == 1


# ── cross-cutting ────────────────────────────────────────────────────────────────────────────


def test_usage_error_exits_1_not_2(db, capsys, monkeypatch) -> None:
    """2 is reserved for SQL failures in the db/ exit-code contract; argparse's default 2 on a
    usage error would read as 'go look at the database'."""
    assert mo.main(["seed"]) == mo.EXIT_VALIDATION  # missing --email
    assert mo.main(["no-such-command"]) == mo.EXIT_VALIDATION
    capsys.readouterr()


def test_connection_failure_exits_3(capsys, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:wrong@127.0.0.1:1/nope")
    monkeypatch.setenv("TOTP_SECRET_ENC_KEY", base64.b64encode(os.urandom(32)).decode())
    assert mo.main(["unlock", "--email", "jared@example.com"]) == mo.EXIT_CONNECTION
    capsys.readouterr()
