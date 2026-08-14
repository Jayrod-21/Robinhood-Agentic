#!/usr/bin/env python3
"""manage_operator.py — the ONLY operator-account lifecycle surface (AUTH_THREAT_MODEL §4, §5.7).

WHY A CLI AND NOT A ROUTE
    There is deliberately no signup route and no self-service password reset (§5.7): an emailed
    reset would make mailbox access equal account access, bypassing TOTP entirely. Recovery is this
    script, run on the host. An attacker who can run it already has host access, which is total
    compromise independently (§6). Consequently every subcommand here is a privileged operation:
    the wrapper (bin/db_manage_operator.sh) runs it inside the migration-runner image on
    `rh-internal` as the DDL role — `rh_auth` deliberately holds no grant to create operators,
    disable them, or set passwords (migration 012).

SUBCOMMANDS
    seed            create an operator: Argon2id password hash, encrypted TOTP secret
                    (confirmed_at = now() — the URI printed below IS the enrolment; there is no
                    in-app confirm route, so a pending row would strand the account), 10 recovery
                    codes. Prints the otpauth:// enrolment URI and the plaintext recovery codes
                    EXACTLY ONCE; neither is ever stored or printed again.
    disable         tombstone the account (disabled_at = now(); operators are disabled, never
                    deleted) and revoke every live session.
    unlock          clear the §5.8 TOTP lockout: locked_until -> NULL, failed_attempts -> 0.
    reset-password  replace the password hash and revoke every live session.
    reset-totp      replace the TOTP secret (new secret confirmed immediately, same reasoning as
                    seed; replay high-water mark and lockout counters reset), replace ALL
                    recovery codes (§5.5 — stale codes must not survive a re-enrolment), and
                    revoke every live session. Prints the new URI + codes once.

CRYPTO — WHAT IS STORED, EXACTLY
    * Passwords: Argon2id via THE single shared PASSWORD_HASHER in
      backend/app/services/crypto.py (time_cost=3, memory_cost=65536, parallelism=1) — the SAME
      instance the backend's §5.2 dummy verify uses. PHC strings are self-describing, so a hash
      made with different parameters would still verify — but at a different cost than the dummy
      path, turning login latency into an email-existence oracle. Never hash here with a bare
      PasswordHasher(): its default parallelism is 4. Migration 012 CHECK-pins the column to the
      `$argon2id$` prefix, so a bug that reaches the column with plaintext fails at INSERT.
    * TOTP secret: `pyotp.random_base32()`, encrypted by THE single shared implementation in
      backend/app/services/crypto.py — AES-256-GCM under TOTP_SECRET_ENC_KEY (base64 of exactly
      32 bytes, validated on load), stored as base64(nonce(12) || ciphertext || tag(16)),
      standard base64 with padding, no associated data. This module deliberately does NOT
      implement encryption: a second implementation that disagrees by a byte produces operators
      who simply cannot log in. Every encrypt here is round-tripped through decrypt before the
      row is written, so a blob the backend cannot read is never stored.
    * Recovery codes: 10 codes, 10 chars each from the Crockford base32 alphabet (no I/L/O/U —
      50 bits of CSPRNG entropy, unambiguous on paper). Only SHA-256 hex digests are stored
      (§5.5: SHA-256 is correct for 50-bit CSPRNG values and wrong for passwords). The digest is
      of the exact 10-character uppercase string as printed — the CANONICAL form. The backend
      folds operator input to it before hashing (uppercase, separators stripped: Crockford
      base32 is case-insensitive on decode, and grouping is presentation, not entropy), so a
      transcription-faithful entry always matches what is stored here.

AUDIT
    Every mutation appends one auth_events row in the same transaction as the mutation itself
    (event types: operator_seeded, operator_disabled, totp_unlocked, password_reset, totp_reset;
    outcome 'success'). A failed command rolls the event back with the mutation — auth_events
    records what happened, not what was attempted from a host shell the attacker already owns.

WHAT IS NEVER PRINTED
    The password (input is getpass or --password-stdin, never argv — argv is visible in `ps`),
    the encryption key, and the raw TOTP secret outside the one-time otpauth:// URI block that IS
    the enrolment channel.

EMAIL
    uq_operators_email is on lower(email): lookups and inserts here normalise to lowercase, so
    Jared@X and jared@x are the same operator everywhere.

EXIT CODES (matching db/migrate.py and the other db/ tools)
    0 ok · 1 validation (bad input, unknown/duplicate operator, weak password, bad key) ·
    2 SQL failure · 3 connection failure (including missing dependencies).

Usage (via bin/db_manage_operator.sh, which supplies the libpq env + TOTP_SECRET_ENC_KEY):
    bin/db_manage_operator.sh seed --email jared@example.com
    bin/db_manage_operator.sh unlock --email jared@example.com
    echo "$PW" | bin/db_manage_operator.sh reset-password --email jared@example.com --password-stdin
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import os
import re
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The shared crypto implementation lives in the backend package; the repo is mounted whole in the
# runner container, so reach it by path the same way db/tests reaches db/migrate.py.
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - clear message beats a traceback
    print("manage_operator: psycopg (v3) is required. pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(3) from None

try:
    import argon2  # noqa: F401  (argon2-cffi — app.services.crypto's PASSWORD_HASHER needs it)
    import pyotp
except ModuleNotFoundError as exc:  # pragma: no cover
    print(
        f"manage_operator: missing dependency {exc.name!r}. Run via bin/db_manage_operator.sh, "
        "which builds an image with argon2-cffi, pyotp, and cryptography baked in.",
        file=sys.stderr,
    )
    raise SystemExit(3) from None

try:
    from app.services.crypto import (
        PASSWORD_HASHER,
        EncryptionKeyError,
        decrypt_totp_secret,
        encrypt_totp_secret,
        load_enc_key,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    print(
        "manage_operator: cannot import backend/app/services/crypto.py "
        f"({exc}). The repo mount must include backend/ — do NOT reimplement the cipher here; "
        "a divergent second implementation produces operators who cannot log in.",
        file=sys.stderr,
    )
    raise SystemExit(3) from None

logger = logging.getLogger("manage_operator")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

# §5.1: minimum length 12 per NIST SP 800-63B (block-list, not composition rules); 256-byte cap so
# an over-long password cannot turn 64 MiB Argon2 hashing into a DoS primitive.
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_BYTES = 256

# §5.5: Crockford base32 — 32 symbols, no I/L/O/U, so a code transcribed from paper has no 1/I or
# 0/O ambiguity. 10 symbols × 5 bits = 50 bits of CSPRNG entropy per code.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RECOVERY_CODE_LEN = 10
RECOVERY_CODE_COUNT = 10

# Shown in the authenticator app next to the account name; cosmetic, but keep it stable — a
# changed issuer looks like a different account to the operator.
TOTP_ISSUER = "ww.jaredstudio.com"

# Mirrors ck_operators_email exactly, so bad input fails here with a message instead of at INSERT
# with a constraint name.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LEN = 254

# Written to auth_events.user_agent so CLI mutations are distinguishable from app mutations when
# the backend starts writing events. No IP: this runs on the host, there is no meaningful peer.
CLI_USER_AGENT = "bin/manage_operator.py"

# §5.1's local common-password list. Every entry under 12 characters is already unreachable past
# the length floor, so only >= 12-char members of the usual breach-corpus toplists are kept.
# Checked case-insensitively. Deliberately includes project-guessable strings.
COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "password12345",
        "password123456",
        "password1234567",
        "password12345678",
        "passw0rd12345",
        "p@ssword12345",
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789012345",
        "qwertyuiop123",
        "qwertyuiopasdfgh",
        "qwerty123456",
        "administrator",
        "administrator1",
        "letmein12345",
        "welcome123456",
        "iloveyou12345",
        "sunshine12345",
        "princess12345",
        "football12345",
        "baseball12345",
        "superman12345",
        "dragondragon",
        "trustno1trustno1",
        "correcthorsebatterystaple",
        "wasdenwatch123",
        "jaredstudio123",
        "robinhood1234",
    }
)


class CliError(Exception):
    """Validation failure: bad input or an impossible request. Maps to exit 1, never a traceback."""


# ── input handling ────────────────────────────────────────────────────────────────────────────


def validate_password(password: str) -> None:
    """§5.1 password policy. Raises CliError; never echoes the candidate."""
    if len(password) < MIN_PASSWORD_CHARS:
        raise CliError(f"password must be at least {MIN_PASSWORD_CHARS} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        # The cap exists so Argon2's 64 MiB memory cost cannot be multiplied by a huge input.
        raise CliError(f"password exceeds the {MAX_PASSWORD_BYTES}-byte cap")
    if password.lower() in COMMON_PASSWORDS:
        raise CliError("password is on the common-password block list — pick something else")


def read_password(password_stdin: bool) -> str:
    """Read and validate the new password. Never from argv (argv is world-readable in /proc)."""
    if password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise CliError("--password-stdin given but stdin is empty")
        password = line.rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            raise CliError("passwords do not match")
    validate_password(password)
    return password


def normalise_email(email: str) -> str:
    """Lowercase + validate. uq_operators_email is on lower(email); normalising on every insert
    AND lookup is what makes that index the single source of identity."""
    addr = email.strip().lower()
    if not EMAIL_RE.match(addr) or len(addr) > MAX_EMAIL_LEN:
        raise CliError(f"not a valid email address: {email!r}")
    return addr


# ── crypto material ───────────────────────────────────────────────────────────────────────────


def generate_recovery_codes() -> list[str]:
    """10 unique 50-bit Crockford-base32 codes. Uniqueness is re-checked because two identical
    codes would collide on uq_recovery_codes_hash at INSERT — vanishingly unlikely, but a retry
    here is free and a constraint error at seed time is not."""
    codes: set[str] = set()
    while len(codes) < RECOVERY_CODE_COUNT:
        codes.add("".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(RECOVERY_CODE_LEN)))
    return sorted(codes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_encrypted_secret(key: bytes) -> tuple[str, str]:
    """Generate a fresh TOTP secret and its stored ciphertext.

    The round-trip check is the whole point: this CLI and the backend MUST agree on the format,
    and the cheapest place to prove the stored blob is decryptable is before it is stored. A blob
    that fails its own round-trip is never written.
    """
    secret = pyotp.random_base32()
    blob = encrypt_totp_secret(secret, key)
    if decrypt_totp_secret(blob, key) != secret:  # pragma: no cover - would be a crypto.py bug
        raise CliError("encrypt/decrypt round-trip failed — refusing to store an unreadable secret")
    return secret, blob


def print_enrolment_material(email: str, secret: str, codes: list[str]) -> None:
    """The single place plaintext enrolment material is shown. Printed once, never persisted.

    MANUAL ENTRY IS THE PRIMARY PATH, not a fallback. This is a terminal on a headless host: it
    cannot draw a QR code, and the first version of this function printed a bare otpauth:// URI
    under the instruction "Scan it now" — telling the operator to do the one thing the output made
    impossible. The secret is right there inside the URI as the `secret=` parameter, but nothing
    said so, and an operator who does not already know the otpauth format has no way to enrol.

    So the fields every authenticator's "enter a setup key" screen asks for are printed
    explicitly, and the key is grouped in fours because it is being typed by hand off a screen.
    The URI stays for anyone piping it into a QR renderer.
    """
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)
    grouped = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    print()
    print("TOTP enrolment — shown ONCE, never stored in plaintext and never shown again.")
    print()
    print("  MANUAL ENTRY (in your authenticator: 'Enter a setup key' / 'Enter key manually')")
    print(f"    Account name : {email}")
    print(f"    Issuer       : {TOTP_ISSUER}")
    print(f"    Key          : {grouped}")
    print("    Type         : Time-based (TOTP), SHA-1, 6 digits, 30-second period")
    print("                   — these are the defaults in every mainstream app; change nothing.")
    print()
    print("  Or scan this URI as a QR code (render it locally — do not paste it into a website,")
    print("  a QR generator you do not control receives your second factor):")
    print(f"    {uri}")
    print()
    print("Recovery codes — shown ONCE. Each works exactly once; store them offline:")
    for i, code in enumerate(codes, 1):
        print(f"  {i:2d}. {code}")
    print()
    print("This output will not be shown again. Losing both the authenticator and these codes")
    print("means running `reset-totp` on the host.")


# ── database helpers ──────────────────────────────────────────────────────────────────────────


def find_operator(conn: psycopg.Connection, email: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT id, email FROM operators WHERE lower(email) = %s", (email,)
    ).fetchone()
    if row is None:
        raise CliError(f"no operator with email {email!r}")
    return row[0], row[1]


def log_event(conn: psycopg.Connection, operator_id: int | None, event_type: str) -> None:
    """One audit row per mutation, in the SAME transaction — commits with it, rolls back with it.
    event_type/outcome satisfy 012's ^[a-z0-9_]{2,64}$ / ^[a-z0-9_]{2,32}$ CHECKs."""
    conn.execute(
        "INSERT INTO auth_events (operator_id, event_type, outcome, user_agent)"
        " VALUES (%s, %s, %s, %s)",
        (operator_id, event_type, "success", CLI_USER_AGENT),
    )


def revoke_sessions(conn: psycopg.Connection, operator_id: int) -> int:
    """Stamp revoked_at on every live session (§11.5: revocation is a stamp, never a DELETE)."""
    cur = conn.execute(
        "UPDATE sessions SET revoked_at = now()"
        " WHERE operator_id = %s AND revoked_at IS NULL",
        (operator_id,),
    )
    return cur.rowcount


# ── subcommands ───────────────────────────────────────────────────────────────────────────────


def cmd_seed(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    # Key first: fail on a bad TOTP_SECRET_ENC_KEY before prompting anyone for a password.
    key = load_enc_key()
    if conn.execute("SELECT 1 FROM operators WHERE lower(email) = %s", (email,)).fetchone():
        raise CliError(f"operator {email!r} already exists (operators are disabled, never deleted"
                       " — use disable / reset-password / reset-totp instead)")
    password = read_password(args.password_stdin)
    # THE shared hasher (§5.1/§5.2): hashing with any other parameters (e.g. a bare
    # PasswordHasher(), whose default parallelism is 4) makes real verifications run at a
    # different cost than the backend's dummy verify — a timing oracle for email existence.
    password_hash = PASSWORD_HASHER.hash(password)
    secret, blob = make_encrypted_secret(key)
    codes = generate_recovery_codes()

    try:
        operator_id = conn.execute(
            "INSERT INTO operators (email, password_hash) VALUES (%s, %s) RETURNING id",
            (email, password_hash),
        ).fetchone()[0]
    except psycopg.errors.UniqueViolation:
        # The pre-check above races with a concurrent seed; the unique index on lower(email) is
        # the real gate, so translate its refusal into the same friendly message.
        raise CliError(f"operator {email!r} already exists") from None
    # confirmed_at = now(): possession of the secret is proven OUT-OF-BAND, by this very run —
    # the otpauth:// URI is printed once, below, to the person invoking the CLI on the host, and
    # there is deliberately no in-app enrol/confirm route (§4/§5.7: the CLI is the only account
    # lifecycle surface). Login reads only confirmed enrolments (§5.4), so leaving this NULL
    # would strand every seeded operator: password accepted, second factor unreachable, no route
    # anywhere to flip the flag. The §5.4 pending-secret rule still stands for any future in-app
    # re-enrolment flow; it just has no unconfirmed rows to apply to from this path.
    conn.execute(
        "INSERT INTO operator_totp (operator_id, secret_encrypted, confirmed_at) "
        "VALUES (%s, %s, now())",
        (operator_id, blob),
    )
    for code in codes:
        conn.execute(
            "INSERT INTO recovery_codes (operator_id, code_hash) VALUES (%s, %s)",
            (operator_id, sha256_hex(code)),
        )
    log_event(conn, operator_id, "operator_seeded")

    print(f"Seeded operator id={operator_id} email={email}")
    print_enrolment_material(email, secret, codes)
    return EXIT_OK


def cmd_disable(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    operator_id, _ = find_operator(conn, email)
    cur = conn.execute(
        "UPDATE operators SET disabled_at = now() WHERE id = %s AND disabled_at IS NULL",
        (operator_id,),
    )
    revoked = revoke_sessions(conn, operator_id)
    if cur.rowcount == 0:
        # Idempotent: already-disabled is a no-op, not an error — but say so, and still sweep
        # sessions in case a previous run was interrupted between the two statements.
        print(f"Operator {email} was already disabled ({revoked} lingering session(s) revoked)")
        return EXIT_OK
    log_event(conn, operator_id, "operator_disabled")
    print(f"Disabled operator id={operator_id} email={email}; revoked {revoked} live session(s)")
    return EXIT_OK


def cmd_unlock(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    operator_id, _ = find_operator(conn, email)
    cur = conn.execute(
        "UPDATE operator_totp SET locked_until = NULL, failed_attempts = 0"
        " WHERE operator_id = %s",
        (operator_id,),
    )
    if cur.rowcount == 0:
        raise CliError(f"operator {email!r} has no TOTP enrolment row — nothing to unlock")
    log_event(conn, operator_id, "totp_unlocked")
    print(f"Unlocked operator id={operator_id} email={email}"
          " (locked_until cleared, failed_attempts reset to 0)")
    return EXIT_OK


def cmd_reset_password(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    operator_id, _ = find_operator(conn, email)
    password = read_password(args.password_stdin)
    conn.execute(
        "UPDATE operators SET password_hash = %s WHERE id = %s",
        (PASSWORD_HASHER.hash(password), operator_id),  # THE shared hasher — see cmd_seed
    )
    # §5.3: a password reset means the old credential may be compromised; every session minted
    # under it goes with it.
    revoked = revoke_sessions(conn, operator_id)
    log_event(conn, operator_id, "password_reset")
    print(f"Password reset for operator id={operator_id} email={email};"
          f" revoked {revoked} live session(s)")
    return EXIT_OK


def cmd_reset_totp(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    email = normalise_email(args.email)
    key = load_enc_key()
    operator_id, _ = find_operator(conn, email)
    secret, blob = make_encrypted_secret(key)
    codes = generate_recovery_codes()

    # Replace the enrolment wholesale: new secret, replay high-water mark and lockout counters
    # reset — stale state from the old secret must not gate the new one. confirmed_at = now()
    # for the same reason as cmd_seed: the new URI is printed once to the host operator below,
    # which IS the possession proof, and no in-app confirm route exists — clearing the flag here
    # would strand the account at the second factor immediately after every reset.
    cur = conn.execute(
        "UPDATE operator_totp SET secret_encrypted = %s, confirmed_at = now(),"
        " last_used_step = 0, failed_attempts = 0, locked_until = NULL"
        " WHERE operator_id = %s",
        (blob, operator_id),
    )
    if cur.rowcount == 0:
        # A seeded operator always has a row, but repair the gap rather than strand the account.
        conn.execute(
            "INSERT INTO operator_totp (operator_id, secret_encrypted, confirmed_at) "
            "VALUES (%s, %s, now())",
            (operator_id, blob),
        )
    # §5.5: recovery codes bypass TOTP by design, so codes issued alongside the OLD secret must
    # not survive its replacement — an attacker who captured them would keep a way in past the
    # re-enrolment. Same transaction, used and unused alike.
    conn.execute("DELETE FROM recovery_codes WHERE operator_id = %s", (operator_id,))
    for code in codes:
        conn.execute(
            "INSERT INTO recovery_codes (operator_id, code_hash) VALUES (%s, %s)",
            (operator_id, sha256_hex(code)),
        )
    # A TOTP reset means "assume the old factor (and anything minted under it) is not mine".
    revoked = revoke_sessions(conn, operator_id)
    log_event(conn, operator_id, "totp_reset")

    print(f"TOTP reset for operator id={operator_id} email={email};"
          f" re-enrolment required; revoked {revoked} live session(s)")
    print_enrolment_material(email, secret, codes)
    return EXIT_OK


COMMANDS = {
    "seed": cmd_seed,
    "disable": cmd_disable,
    "unlock": cmd_unlock,
    "reset-password": cmd_reset_password,
    "reset-totp": cmd_reset_totp,
}


# ── entrypoint ────────────────────────────────────────────────────────────────────────────────


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors; 2 is reserved for SQL failures in the db/ exit-code
    contract, so usage errors are remapped to validation (1) — same pattern as db/migrate.py."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_VALIDATION)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="manage_operator", description="3b operator-account lifecycle CLI (host-only)")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name, needs_password in (
        ("seed", True),
        ("disable", False),
        ("unlock", False),
        ("reset-password", True),
        ("reset-totp", False),
    ):
        sp = sub.add_parser(name)
        sp.add_argument("--email", required=True, help="operator email (matched case-insensitively)")
        if needs_password:
            sp.add_argument(
                "--password-stdin",
                action="store_true",
                help="read the password from the first line of stdin instead of prompting"
                " (never pass a password as an argument — argv is visible in `ps`)",
            )
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse: 0 for --help, EXIT_VALIDATION for usage errors
        return int(exc.code or 0)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        # DATABASE_URL when set (tests, db_mark.sh-style callers); otherwise an empty conninfo
        # lets libpq read PGHOST/PGUSER/PGPASSWORD/PGDATABASE — how the wrapper passes
        # credentials without ever putting them in argv.
        conn = psycopg.connect(os.environ.get("DATABASE_URL", ""))
    except psycopg.OperationalError as exc:
        logger.error("could not connect: %s", exc)
        return EXIT_CONNECTION

    try:
        # `with conn:` commits on clean exit and rolls back on ANY exception — every subcommand
        # is all-or-nothing, audit row included.
        with conn:
            return COMMANDS[args.command](conn, args)
    except (CliError, EncryptionKeyError) as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.Error as exc:
        logger.error("SQL failure: %s", exc)
        return EXIT_SQL


if __name__ == "__main__":
    raise SystemExit(main())
