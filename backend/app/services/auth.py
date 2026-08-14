"""Operator authentication service — the server half of docs/AUTH_THREAT_MODEL.md §4-§5.

Every function here runs against the SECOND database pool (``app.db.auth_connection``, role
``rh_auth``) — never ``connection()``: migration 012 REVOKEs every auth table from ``rh_app``, so
the ordinary pool physically cannot read a password hash, and reaching for it raises
InsufficientPrivilege (§8).

FAIL CLOSED — THE ONE PLACE (§8, invariant §11.6)
    ``_auth_db()`` below is the single point where :class:`app.db.DbUnavailable` is translated
    into a refusal. The rest of the app degrades gracefully when the database is down; the auth
    path must not, because an auth path that "degrades gracefully" when it cannot reach the
    credential store fails toward letting the request through — an authentication bypass.
    :class:`AuthUnavailable` is an ``HTTPException(503)``, so wherever it escapes — a route
    handler or the app-wide dependency — FastAPI answers 503: no session minted, no session
    accepted, never a default-allow. Nothing else in this module (and nothing outside it) may
    catch ``DbUnavailable`` on an auth path.

TRANSACTION SHAPE (why the ``_*_tx`` helpers return outcomes instead of raising)
    ``auth_connection()`` commits on clean exit and rolls back when the body raises. Failure
    bookkeeping — the §5.8 lockout counter, challenge ``attempts``, every failure row in
    ``auth_events`` — must COMMIT precisely when the attempt fails, so a domain failure can never
    be signalled by raising inside the connection block (the raise would roll its own evidence
    back). Each flow therefore computes an outcome inside the transaction, exits cleanly, and the
    public wrapper raises after the commit.

WHAT THE SECOND FACTOR ENFORCES
    * TOTP replay (§5.4): ``operator_totp.last_used_step`` is a monotonic high-water mark. The
      accepted RFC-6238 step must be strictly greater, and the write is rowcount-gated in the same
      transaction that consumes the challenge and mints the session — two concurrent submissions
      of one code produce exactly one session.
    * Drift window (§5.4): ``TOTP_STEP_WINDOW`` below, ±1 step and deliberately NOT config.
    * Lockout (§5.8): 5 strikes / 15 minutes (config-tunable), counted at the TOTP step ONLY —
      reachable only after a correct password, so an attacker without the password cannot lock an
      operator out by spraying an email. A lock answers 423 with ``retry_after``: observable,
      never silent.
    * Recovery codes (§5.5): single-use via a rowcount-gated ``UPDATE … WHERE used_at IS NULL``;
      using one revokes every other session for the operator ("I lost my authenticator" implies
      "assume the other sessions are not mine").

ENUMERATION (§5.2)
    ``password_login`` returns the same ``None`` for unknown email, disabled account, oversize
    password, and wrong password, and the unknown/disabled/oversize branches run the same Argon2id
    work as a real verification via ``_dummy_verify`` so the timing channel closes with the
    response channel. The router serializes ``None`` into one fixed body.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyotp
from argon2 import exceptions as argon2_exceptions
from fastapi import HTTPException, Request

from app.config import get_settings
from app.db import DbUnavailable, auth_connection, get_db_settings
from app.services.crypto import (
    PASSWORD_HASHER,
    SecretDecryptionError,
    decrypt_totp_secret,
    digest_token,
    generate_token,
    tokens_equal,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

logger = logging.getLogger("agentic.auth")

# §5.3 — THE COOKIE NAME IS LOAD-BEARING, NOT COSMETIC. ww.jaredstudio.com shares the registrable
# domain jaredstudio.com with korean./uvrl./uvrl-study. siblings, and a vulnerability in ANY of
# them can set a Domain=jaredstudio.com cookie that the browser would send to us — SameSite does
# not help, because a sibling subdomain is same-site. Browsers refuse any __Host- cookie that
# carries a Domain attribute, is not Secure, or has Path other than /, so a sibling cannot write
# this cookie at all. Never rename it away from the __Host- prefix (invariant §11.9).
SESSION_COOKIE_NAME = "__Host-rh_sid"

# §5.1 — passwords above this take the dummy-verify path: Argon2 never sees oversize input, and
# the response is byte- and work-identical to a wrong password.
_MAX_PASSWORD_BYTES = 256

# §5.4 — the TOTP acceptance window, pinned at ±1 step (±30 s) as a CONSTANT, not config. Widening
# it multiplies the online guessing surface linearly (±5 would quintuple it); a skew complaint is
# fixed by fixing the clock, and changing this number requires a code change and a review.
TOTP_STEP_SECONDS = 30
TOTP_STEP_WINDOW = 1

_TOTP_CODE_SHAPE = re.compile(r"^[0-9]{6}$")
# Crockford base32, uppercase — the canonical form bin/manage_operator.py hashes and prints.
# Crockford base32 is case-insensitive on decode by design, and excluding I/L/O/U is about paper
# transcription — so the OPERATOR'S input is folded to this canonical form first (see
# _canonical_recovery_code): lowercase and grouping separators are presentation, not entropy,
# and must never cost a §5.8 lockout strike off a correctly transcribed printed card.
_RECOVERY_CODE_SHAPE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{10}$")
# Separators an operator plausibly types or a printed card plausibly carries: whitespace and
# hyphens. Nothing from the code alphabet, so stripping them cannot merge two distinct codes.
_RECOVERY_SEPARATORS = re.compile(r"[\s\-]+")
# 32 urlsafe CSPRNG bytes encode to 43 chars; the gate rejects obvious noise before any DB work.
_BEARER_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")

# Paths the app-wide auth dependency exempts (§4): the health probe, and the auth routes
# themselves. NOTHING else — a new router must be covered without anyone remembering (the same
# reasoning as enforce_same_origin's app-wide registration).
_PUBLIC_PATHS = frozenset({"/api/health"})
_AUTH_ROUTE_PREFIX = "/api/auth/"


class AuthUnavailable(HTTPException):
    """The credential store cannot be reached — REFUSE (503). Never a default-allow.

    Raised in exactly one place (``_auth_db``); subclassing ``HTTPException`` means the refusal
    holds wherever it escapes, route handler or app-wide dependency alike, with no second
    translation layer to forget.
    """

    def __init__(self, detail: str = "authentication is temporarily unavailable") -> None:
        super().__init__(status_code=503, detail=detail)


class SecondFactorRejected(Exception):
    """The TOTP/recovery step failed. Deliberately reason-free: wrong code, replayed code, dead
    challenge, and unenrolled operator must all look identical to the caller (§5.2, §5.4)."""


class AccountLocked(Exception):
    """§5.8 lockout: observable, never silent — carries the remaining wait for the 423 body."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"account locked; retry in {retry_after}s")
        self.retry_after = retry_after


class VerificationRejected(Exception):
    """An email-verification token was refused. This flow sits AFTER the email round-trip, so
    §5.2 does not apply and the reason is deliberately specific (the frontend maps it)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IssuedChallenge:
    """The password step's ONLY product (§4): never a session, only the right to attempt TOTP."""

    token: str
    expires_in: int


@dataclass(frozen=True)
class OperatorView:
    """What an authenticated request may know about its operator."""

    operator_id: int
    email: str
    email_verified: bool


@dataclass(frozen=True)
class ResendDecision:
    """Outcome of a resend request. ``send=False`` (unknown address, already verified, cooldown)
    is invisible in the HTTP response (§5.2/§5.6) — the router answers 202 either way."""

    send: bool
    email: str | None = None
    token: str | None = None
    ttl_hours: int = 24


@dataclass(frozen=True)
class _MfaOutcome:
    kind: str  # "ok" | "rejected" | "locked" | "unavailable"
    session_token: str | None = None
    retry_after: int = 0


@dataclass(frozen=True)
class _VerifyOutcome:
    kind: str  # "verified" | "already_verified" | "rejected"
    reason: str | None = None


# --------------------------------------------------------------------------------------------
# The fail-closed database boundary (§8, invariant §11.6).
# --------------------------------------------------------------------------------------------


@contextmanager
def _auth_db() -> Iterator[psycopg.Connection]:
    """An rh_auth connection, with DbUnavailable translated to a 503 REFUSAL.

    This is the single fail-closed translation point named by ``pool.py::auth_connection``'s
    docstring. Commit-on-clean-exit / rollback-on-raise semantics come from the pool.
    """
    try:
        with auth_connection() as conn:
            yield conn
    except DbUnavailable as exc:
        # str(exc) is safe to surface: pool.py guarantees it never contains the DSN.
        raise AuthUnavailable(str(exc)) from exc


# --------------------------------------------------------------------------------------------
# Password verification (§5.1, §5.2).
# --------------------------------------------------------------------------------------------

# §5.1/§5.2 — THE shared hasher from crypto.py, the same instance bin/manage_operator.py hashes
# with. Sharing it is load-bearing for the timing half of §5.2: stored PHC strings are
# self-describing, so a real verification runs at the STORED hash's parameters while the dummy
# verify below runs at THIS hasher's parameters — if the two ever diverge (as they did when the
# CLI used argon2-cffi's p=4 defaults against this module's pinned p=1), the difference is a
# per-request oracle that answers "does this email exist?". crypto.py owns the pinned numbers.
_hasher = PASSWORD_HASHER

# A fixed Argon2id hash so the "user not found" branch performs the same 64 MiB of work as the
# "user found, wrong password" branch (§5.2). Computed once at import; the input is arbitrary.
_DUMMY_HASH = _hasher.hash("dummy-hash-for-timing-equalisation")


def _dummy_verify() -> None:
    """Burn one real Argon2id verification against the fixed hash. Always 'fails'; never raises.

    Called with a CONSTANT candidate on purpose: the oversize-password path (§5.1) must never
    hand attacker-sized input to Argon2, and Argon2's cost is dominated by its memory/time
    parameters rather than the candidate length, so the timing still matches a genuine mismatch.
    """
    try:
        _hasher.verify(_DUMMY_HASH, "not-the-dummy-password")
    except argon2_exceptions.VerifyMismatchError:
        pass


def _verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except argon2_exceptions.VerifyMismatchError:
        return False
    except (argon2_exceptions.VerificationError, argon2_exceptions.InvalidHashError):
        # A malformed stored hash is a data problem, not a caller problem — refuse, loudly.
        logger.error("operator password hash failed to parse during verification")
        return False


# --------------------------------------------------------------------------------------------
# TOTP matching (§5.4).
# --------------------------------------------------------------------------------------------


def _time() -> float:
    """Clock hook for the TOTP step computation; tests pin it instead of sleeping."""
    return time.time()


def _match_totp_step(secret: str, code: str) -> int | None:
    """Return the RFC-6238 step the code matches within ±TOTP_STEP_WINDOW, else None.

    Returning the STEP (not a bool) is what makes the §5.4 replay guard possible: the caller
    requires ``step > last_used_step``, so a code from an earlier step can never be accepted after
    a later one — the skew window does not allow walking backwards.
    """
    totp = pyotp.TOTP(secret)
    current = int(_time() // TOTP_STEP_SECONDS)
    for offset in range(-TOTP_STEP_WINDOW, TOTP_STEP_WINDOW + 1):
        step = current + offset
        if step >= 0 and hmac.compare_digest(totp.at(step * TOTP_STEP_SECONDS), code):
            return step
    return None


def _canonical_totp_code(raw: str) -> str:
    """Fold operator input to the bare 6 digits, for exactly the reason recovery codes are folded.

    Every mainstream authenticator RENDERS the code grouped — Google Authenticator, Authy and
    Microsoft Authenticator all show "123 456" — and operators type what they see. Without this,
    a *correct* code carrying the space the app displayed fails `_TOTP_CODE_SHAPE`, matches no
    branch, and is scored as a wrong guess: indistinguishable in outcome AND in the §5.8 lockout
    counter from an attacker's miss. Five of them lock the operator out of a live brokerage view
    on input that was right every time.

    The separator class is disjoint from the digits, so stripping it cannot turn one valid code
    into another, and it costs no entropy — grouping is presentation. This is the same reasoning
    as _canonical_recovery_code; the fix that introduced that folding covered only one of the two
    code types that enter the same login field.
    """
    return _RECOVERY_SEPARATORS.sub("", raw.strip())


def _canonical_recovery_code(raw: str) -> str:
    """Fold operator input to the canonical form the CLI hashed: uppercase, no separators.

    Crockford base32 decodes case-insensitively by definition, and grouping separators (spaces,
    hyphens) are how humans transcribe a 10-character code off a printed card — neither carries
    entropy, and the alphabet has no case collisions, so this folding cannot merge two distinct
    codes. Without it, 'abcdefgh01' and 'ABCDE-FGH01' each COUNT AS A FAILED ATTEMPT for the
    §5.8 lockout — five such typos lock an operator out of a live brokerage view, the exact
    guardrail failure SENIOR_ENGINEER_BAR §7.2 exists to prevent.
    """
    return _RECOVERY_SEPARATORS.sub("", raw.strip()).upper()


# --------------------------------------------------------------------------------------------
# Audit (§5.12) and request-metadata helpers.
# --------------------------------------------------------------------------------------------


def _log_event(
    conn: psycopg.Connection,
    operator_id: int | None,
    event_type: str,
    outcome: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Append one auth_events row in the CALLER's transaction, so the event commits (or rolls
    back) with the state change it records. Emails, tokens, codes, and secrets never enter it."""
    conn.execute(
        "INSERT INTO auth_events (operator_id, event_type, outcome, ip, user_agent) "
        "VALUES (%s, %s, %s, %s::inet, %s)",
        (operator_id, event_type, outcome, ip, _clip_user_agent(user_agent)),
    )


def _clip_user_agent(user_agent: str | None) -> str | None:
    # ck_*_user_agent caps at 512; clip rather than refuse — the UA is metadata, not a gate.
    return user_agent[:512] if user_agent else None


def client_ip(request: Request) -> str | None:
    """The direct peer address as a valid INET literal, or None (e.g. TestClient's 'testclient').

    Deliberately NOT X-Forwarded-For: that header is attacker-writable unless the proxy chain is
    trusted end-to-end, and a forged value would poison the audit log it exists to serve.
    """
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------
# Step 1 — password → challenge (§4, §5.1, §5.2).
# --------------------------------------------------------------------------------------------


def password_login(
    email: str, password: str, *, ip: str | None = None, user_agent: str | None = None
) -> IssuedChallenge | None:
    """Verify the password and mint a purpose-scoped, single-use, short-TTL challenge.

    NEVER a session (§4). Returns None for every failure — unknown address, disabled account,
    oversize password, wrong password — and every failure branch performs one full Argon2id
    verification, so neither the response nor its timing distinguishes them (§5.2).
    """
    normalized = email.strip().lower()  # uq_operators_email is on lower(email)
    settings = get_settings()
    with _auth_db() as conn:
        row = conn.execute(
            "SELECT id, password_hash, disabled_at FROM operators WHERE lower(email) = %s",
            (normalized,),
        ).fetchone()
        oversize = len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES
        if row is None or row[2] is not None or oversize:
            _dummy_verify()
            _log_event(conn, row[0] if row else None, "login_password", "failure", ip, user_agent)
            return None
        operator_id, password_hash = row[0], row[1]
        if not _verify_password(password_hash, password):
            _log_event(conn, operator_id, "login_password", "failure", ip, user_agent)
            return None
        token = generate_token()
        ttl = settings.auth_challenge_ttl_seconds
        conn.execute(
            "INSERT INTO mfa_login_challenges (operator_id, token_hash, purpose, expires_at) "
            "VALUES (%s, %s, 'login', now() + make_interval(secs => %s))",
            (operator_id, digest_token(token), ttl),
        )
        _log_event(conn, operator_id, "login_password", "success", ip, user_agent)
        return IssuedChallenge(token=token, expires_in=ttl)


# --------------------------------------------------------------------------------------------
# Step 2 — challenge + TOTP/recovery code → session (§5.4, §5.5, §5.8).
# --------------------------------------------------------------------------------------------


def complete_mfa(
    challenge_token: str, code: str, *, ip: str | None = None, user_agent: str | None = None
) -> str:
    """Complete the second step and mint a session token (the value for the __Host- cookie).

    Raises :class:`AccountLocked` (423), :class:`SecondFactorRejected` (401), or
    :class:`AuthUnavailable` (503). Raising happens AFTER the transaction commits, so failure
    bookkeeping (lockout counters, attempts, audit rows) always lands — see the module docstring.
    """
    with _auth_db() as conn:
        outcome = _complete_mfa_tx(conn, challenge_token, code, ip, user_agent)
    if outcome.kind == "locked":
        raise AccountLocked(outcome.retry_after)
    if outcome.kind == "unavailable":
        raise AuthUnavailable()
    if outcome.kind != "ok" or outcome.session_token is None:
        raise SecondFactorRejected()
    return outcome.session_token


def _complete_mfa_tx(
    conn: psycopg.Connection,
    challenge_token: str,
    code: str,
    ip: str | None,
    user_agent: str | None,
) -> _MfaOutcome:
    settings = get_settings()
    rejected = _MfaOutcome(kind="rejected")

    token_clean = challenge_token.strip()
    if not _BEARER_TOKEN_SHAPE.fullmatch(token_clean):
        _log_event(conn, None, "mfa_totp", "invalid_challenge", ip, user_agent)
        return rejected

    # Purpose-scoped lookup (§5.4): an 'enroll' challenge is invisible to the login route.
    challenge = conn.execute(
        "SELECT c.id, c.operator_id, c.consumed_at, (c.expires_at > now()) AS unexpired, "
        "       c.token_hash, o.disabled_at "
        "FROM mfa_login_challenges c JOIN operators o ON o.id = c.operator_id "
        "WHERE c.token_hash = %s AND c.purpose = 'login'",
        (digest_token(token_clean),),
    ).fetchone()
    if (
        challenge is None
        or not tokens_equal(digest_token(token_clean), challenge[4])  # defense-in-depth
        or challenge[2] is not None  # consumed: single-use (§4)
        or not challenge[3]  # expired
        or challenge[5] is not None  # operator disabled
    ):
        _log_event(
            conn,
            challenge[1] if challenge else None,
            "mfa_totp",
            "invalid_challenge",
            ip,
            user_agent,
        )
        return rejected
    challenge_id, operator_id = challenge[0], challenge[1]

    # §5.4: login reads ONLY confirmed enrolments — a pending secret can never satisfy a login.
    totp_row = conn.execute(
        "SELECT secret_encrypted, last_used_step, failed_attempts, "
        "       CASE WHEN locked_until IS NOT NULL AND locked_until > now() "
        "            THEN CEIL(EXTRACT(EPOCH FROM (locked_until - now())))::int ELSE 0 END "
        "FROM operator_totp WHERE operator_id = %s AND confirmed_at IS NOT NULL",
        (operator_id,),
    ).fetchone()
    if totp_row is None:
        _log_event(conn, operator_id, "mfa_totp", "no_enrolment", ip, user_agent)
        return rejected
    secret_encrypted, last_used_step, failed_attempts, lock_remaining = totp_row

    # §5.8: the lockout gate runs BEFORE any verification work.
    if lock_remaining > 0:
        _log_event(conn, operator_id, "mfa_totp", "locked", ip, user_agent)
        return _MfaOutcome(kind="locked", retry_after=int(lock_remaining))

    conn.execute(
        "UPDATE mfa_login_challenges SET attempts = attempts + 1 WHERE id = %s", (challenge_id,)
    )

    code_clean = code.strip()
    totp_candidate = _canonical_totp_code(code_clean)
    recovery_candidate = _canonical_recovery_code(code_clean)
    matched = False
    via_recovery = False
    challenge_consumed = False
    if _TOTP_CODE_SHAPE.fullmatch(totp_candidate):
        try:
            secret = decrypt_totp_secret(secret_encrypted)
        except SecretDecryptionError:
            # Wrong key or tampered ciphertext: a server-side fault, never "trust the plaintext"
            # and never a normal 401 — the operator cannot fix it by retyping a code.
            logger.error("TOTP secret decryption failed for operator %s", operator_id)
            _log_event(conn, operator_id, "mfa_totp", "error", ip, user_agent)
            return _MfaOutcome(kind="unavailable")
        step = _match_totp_step(secret, totp_candidate)
        if step is not None and step > last_used_step:
            # §5.4 replay guard: the monotonic high-water mark, rowcount-gated so two concurrent
            # submissions of the same code advance it exactly once (one session, not two).
            advanced = conn.execute(
                "UPDATE operator_totp SET last_used_step = %s, failed_attempts = 0, "
                "locked_until = NULL WHERE operator_id = %s AND last_used_step < %s",
                (step, operator_id, step),
            ).rowcount
            if advanced != 1:
                # The code WAS valid for a step above this transaction's snapshot of the mark —
                # the row moved underneath us, i.e. a concurrent submission of the same code won
                # the gate between our SELECT and this UPDATE. That is a double-click's loser
                # holding the RIGHT code, not a wrong guess: rejected, but never a §5.8 strike
                # (five fast double-clicks must not lock the operator out). A genuine replay —
                # resubmitting a code AFTER a win committed — reads the advanced mark in its own
                # SELECT, fails `step > last_used_step` above, and still counts as a failure.
                _log_event(conn, operator_id, "mfa_totp", "replayed", ip, user_agent)
                return rejected
            matched = True
    elif _RECOVERY_CODE_SHAPE.fullmatch(recovery_candidate):
        # ORDER IS LOAD-BEARING (§4/§5.5): consume the CHALLENGE before anything destructive.
        # The challenge is the cheapest single-use gate and the serialisation point for the whole
        # step — when it was consumed only at the end, two concurrent submissions carrying two
        # DIFFERENT valid codes each burned their code and revoked every session (this module's
        # transaction shape COMMITS domain rejections), while only one won the challenge. The
        # loser must lose HERE, before its code is spent or a single session is touched.
        # Deliberate trade-off: a mistyped-but-shaped recovery code now spends the challenge, so
        # the operator re-enters the password they typed seconds ago — cheap, and the canonical
        # folding above already absorbs the likely typos (case, separators).
        challenge_consumed = (
            conn.execute(
                "UPDATE mfa_login_challenges SET consumed_at = now() "
                "WHERE id = %s AND consumed_at IS NULL",
                (challenge_id,),
            ).rowcount
            == 1
        )
        if not challenge_consumed:
            _log_event(conn, operator_id, "mfa_recovery", "invalid_challenge", ip, user_agent)
            return rejected
        # §5.5 single-use, enforced AT THE DATABASE: a racing double-submit consumes at most once.
        consumed = conn.execute(
            "UPDATE recovery_codes SET used_at = now() "
            "WHERE operator_id = %s AND code_hash = %s AND used_at IS NULL",
            (operator_id, digest_token(recovery_candidate)),
        ).rowcount
        if consumed == 1:
            matched = True
            via_recovery = True
            conn.execute(
                "UPDATE operator_totp SET failed_attempts = 0, locked_until = NULL "
                "WHERE operator_id = %s",
                (operator_id,),
            )
            # §5.5/§5.3: a recovery code means "I lost my authenticator" — assume the other
            # sessions are not the operator's, and revoke them before minting the new one.
            conn.execute(
                "UPDATE sessions SET revoked_at = now() "
                "WHERE operator_id = %s AND revoked_at IS NULL",
                (operator_id,),
            )

    if not matched:
        event = "mfa_recovery" if _RECOVERY_CODE_SHAPE.fullmatch(recovery_candidate) else "mfa_totp"
        failed = failed_attempts + 1
        if failed >= settings.totp_max_failed_attempts:
            # §5.8: the lock is observable (423 + retry_after + audit event), never silent.
            lock_minutes = settings.totp_lockout_minutes
            conn.execute(
                "UPDATE operator_totp SET failed_attempts = %s, "
                "locked_until = now() + make_interval(mins => %s) WHERE operator_id = %s",
                (failed, lock_minutes, operator_id),
            )
            _log_event(conn, operator_id, event, "failure", ip, user_agent)
            _log_event(conn, operator_id, "totp_lockout", "locked", ip, user_agent)
            return _MfaOutcome(kind="locked", retry_after=lock_minutes * 60)
        conn.execute(
            "UPDATE operator_totp SET failed_attempts = %s WHERE operator_id = %s",
            (failed, operator_id),
        )
        _log_event(conn, operator_id, event, "failure", ip, user_agent)
        return rejected

    # Consume the challenge (single-use, §4) — rowcount-gated so a raced duplicate loses here.
    # The recovery branch consumed it up front (its serialisation point); the TOTP branch is
    # already serialised by the last_used_step gate above, so consuming here is safe for it.
    if not challenge_consumed:
        consumed = conn.execute(
            "UPDATE mfa_login_challenges SET consumed_at = now() "
            "WHERE id = %s AND consumed_at IS NULL",
            (challenge_id,),
        ).rowcount
        if consumed != 1:
            _log_event(conn, operator_id, "mfa_totp", "invalid_challenge", ip, user_agent)
            return rejected

    # §5.3 (fixation, structurally): no session identifier exists before this INSERT — the row is
    # created only after the second factor succeeds, and the cookie is set from this fresh token.
    session_token = generate_token()
    conn.execute(
        "INSERT INTO sessions (operator_id, token_hash, expires_at, user_agent, ip) "
        "VALUES (%s, %s, now() + make_interval(hours => %s), %s, %s::inet)",
        (
            operator_id,
            digest_token(session_token),
            settings.auth_session_ttl_hours,
            _clip_user_agent(user_agent),
            ip,
        ),
    )
    _log_event(
        conn, operator_id, "mfa_recovery" if via_recovery else "mfa_totp", "success", ip, user_agent
    )
    return _MfaOutcome(kind="ok", session_token=session_token)


# --------------------------------------------------------------------------------------------
# Sessions (§5.3).
# --------------------------------------------------------------------------------------------


def authenticate_session(token: str | None, *, touch: bool = True) -> OperatorView | None:
    """Validate a presented cookie value; returns the operator or None.

    Absolute expiry AND idle timeout are checked server-side on every request — cookie Max-Age is
    client-controlled and never trusted (§5.3). An unknown token is simply absent from sessions,
    which is what makes fixation structurally impossible: a planted value never validates.

    ``touch`` controls whether a VALID session's ``last_seen_at`` is refreshed. It must reflect
    OPERATOR activity, not request traffic: the dashboard pages poll their data endpoints on SWR
    timers (10-30 s), so refreshing on every request means an unattended open tab renews the 24 h
    idle window forever — the §5.3 idle timeout would never fire inside the 14-day absolute
    expiry, exactly the defence it exists to provide for a machine left logged in. Validation is
    identical either way; only the renewal is conditional.
    """
    if not token or not _BEARER_TOKEN_SHAPE.fullmatch(token):
        return None
    token_hash = digest_token(token)
    settings = get_settings()
    with _auth_db() as conn:
        row = conn.execute(
            "SELECT s.id, s.operator_id, s.revoked_at, s.token_hash, "
            "       (s.expires_at > now()) AS unexpired, "
            "       (s.last_seen_at > now() - make_interval(hours => %s)) AS not_idle, "
            "       o.email, o.email_verified_at, o.disabled_at "
            "FROM sessions s JOIN operators o ON o.id = s.operator_id "
            "WHERE s.token_hash = %s",
            (settings.auth_session_idle_hours, token_hash),
        ).fetchone()
        if row is None:
            return None
        (sid, operator_id, revoked_at, stored_hash, unexpired, not_idle, email, verified_at,
         disabled_at) = row
        if not tokens_equal(token_hash, stored_hash):  # defense-in-depth over the indexed lookup
            return None
        if revoked_at is not None or not unexpired or not not_idle or disabled_at is not None:
            return None
        if touch:
            conn.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE id = %s AND revoked_at IS NULL",
                (sid,),
            )
        return OperatorView(
            operator_id=operator_id, email=email, email_verified=verified_at is not None
        )


def revoke_session(
    token: str | None, *, ip: str | None = None, user_agent: str | None = None
) -> None:
    """Server-side logout (§5.3): stamp revoked_at. Clearing the cookie alone is not a logout.

    Idempotent — revoking an unknown or already-revoked token is a no-op, so logout can always
    answer 204. Rows are stamped, never mutated back or deleted (invariant §11.5).
    """
    if not token or not _BEARER_TOKEN_SHAPE.fullmatch(token):
        return
    with _auth_db() as conn:
        row = conn.execute(
            "UPDATE sessions SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL RETURNING operator_id",
            (digest_token(token),),
        ).fetchone()
        if row is not None:
            _log_event(conn, row[0], "logout", "success", ip, user_agent)


# --------------------------------------------------------------------------------------------
# Email verification (§5.6).
# --------------------------------------------------------------------------------------------


def consume_verification_token(token: str) -> str:
    """Redeem a verification token: stamps email_verified_at and NOTHING else — no session, no
    cookie (§5.6). Returns "verified" or "already_verified"; raises VerificationRejected.
    """
    with _auth_db() as conn:
        outcome = _consume_verification_tx(conn, token)
    if outcome.kind == "rejected":
        raise VerificationRejected(outcome.reason or "invalid_token")
    return outcome.kind


def _consume_verification_tx(conn: psycopg.Connection, token: str) -> _VerifyOutcome:
    token_clean = token.strip()
    if not _BEARER_TOKEN_SHAPE.fullmatch(token_clean):
        # Shape gate before any DB work (§5.6) — obvious noise never reaches a query.
        return _VerifyOutcome(kind="rejected", reason="invalid_token")
    row = conn.execute(
        "SELECT t.id, t.operator_id, lower(t.email), t.consumed_at, t.invalidated_at, "
        "       (t.expires_at > now()) AS unexpired, t.token_hash, "
        "       lower(o.email), o.email_verified_at "
        "FROM email_verification_tokens t JOIN operators o ON o.id = t.operator_id "
        "WHERE t.token_hash = %s",
        (digest_token(token_clean),),
    ).fetchone()
    if row is None or not tokens_equal(digest_token(token_clean), row[6]):
        _log_event(conn, None, "email_verify", "failure", None, None)
        return _VerifyOutcome(kind="rejected", reason="invalid_token")
    (token_id, operator_id, attested_email, consumed_at, invalidated_at, unexpired, _,
     current_email, verified_at) = row

    if consumed_at is not None:
        # A double-click loser resolves to a friendly already_verified (§5.6) — but only when the
        # account really is verified; otherwise the token is simply spent.
        if verified_at is not None:
            return _VerifyOutcome(kind="already_verified")
        _log_event(conn, operator_id, "email_verify", "failure", None, None)
        return _VerifyOutcome(kind="rejected", reason="consumed")
    if invalidated_at is not None:
        _log_event(conn, operator_id, "email_verify", "failure", None, None)
        return _VerifyOutcome(kind="rejected", reason="invalid_token")
    if not unexpired:
        _log_event(conn, operator_id, "email_verify", "failure", None, None)
        return _VerifyOutcome(kind="rejected", reason="token_expired")
    if attested_email != current_email:
        # §5.6 address binding — the load-bearing defense against the A → B → A stale-verification
        # replay; holds even if a supersession was ever lost to a crash.
        _log_event(conn, operator_id, "email_verify", "failure", None, None)
        return _VerifyOutcome(kind="rejected", reason="stale_address")

    consumed = conn.execute(
        "UPDATE email_verification_tokens SET consumed_at = now() "
        "WHERE id = %s AND consumed_at IS NULL",
        (token_id,),
    ).rowcount
    if consumed != 1:  # lost the race — at-most-once consumption (§5.6)
        return _VerifyOutcome(kind="already_verified")
    conn.execute(
        "UPDATE operators SET email_verified_at = COALESCE(email_verified_at, now()) "
        "WHERE id = %s",
        (operator_id,),
    )
    _log_event(conn, operator_id, "email_verify", "success", None, None)
    return _VerifyOutcome(kind="verified")


def request_verification_resend(email: str) -> ResendDecision:
    """Decide whether a resend actually sends, atomically with the token insert (§5.6).

    The cooldown probe and the insert run inside one transaction opened with ``FOR UPDATE`` on
    the operator row, so a burst of concurrent resends serializes instead of racing through a
    check-then-act window (the partial unique index is the structural backstop). Suppression —
    unknown address, disabled, already verified, cooldown — is invisible in the response and
    logged server-side only (§5.2).
    """
    normalized = email.strip().lower()
    settings = get_settings()
    ttl_hours = settings.auth_verification_ttl_hours
    with _auth_db() as conn:
        row = conn.execute(
            "SELECT id, email, email_verified_at, disabled_at FROM operators "
            "WHERE lower(email) = %s FOR UPDATE",
            (normalized,),
        ).fetchone()
        if row is None or row[2] is not None or row[3] is not None:
            _log_event(
                conn, row[0] if row else None, "email_verify_resend", "suppressed", None, None
            )
            return ResendDecision(send=False, ttl_hours=ttl_hours)
        operator_id, current_email = row[0], row[1]
        recently = conn.execute(
            "SELECT coalesce(max(created_at) > now() - make_interval(secs => %s), false) "
            "FROM email_verification_tokens WHERE operator_id = %s",
            (settings.auth_resend_cooldown_seconds, operator_id),
        ).fetchone()[0]
        if recently:
            _log_event(conn, operator_id, "email_verify_resend", "suppressed", None, None)
            return ResendDecision(send=False, ttl_hours=ttl_hours)
        # Supersede every prior live token — only one link is ever redeemable (§5.6), and the
        # partial unique index uq_evt_one_live_per_operator enforces it structurally.
        conn.execute(
            "UPDATE email_verification_tokens SET invalidated_at = now() "
            "WHERE operator_id = %s AND consumed_at IS NULL AND invalidated_at IS NULL",
            (operator_id,),
        )
        token = generate_token()
        conn.execute(
            "INSERT INTO email_verification_tokens (operator_id, token_hash, email, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(hours => %s))",
            (operator_id, digest_token(token), current_email, ttl_hours),
        )
        _log_event(conn, operator_id, "email_verify_resend", "requested", None, None)
        return ResendDecision(send=True, email=current_email, token=token, ttl_hours=ttl_hours)


# --------------------------------------------------------------------------------------------
# App-wide enforcement (§4). Registered in main.py via FastAPI(dependencies=[...]), exactly as
# enforce_same_origin is, so a newly added router is covered without anyone remembering.
# --------------------------------------------------------------------------------------------


_enforcement_warning_emitted = False


def auth_enforcement_configured() -> bool:
    """True when AUTH_DATABASE_URL is set, i.e. session enforcement is actually running.

    Exposed so operational surfaces can REPORT the posture rather than leaving it inferable only
    from behaviour. See :func:`_warn_enforcement_disabled_once`.
    """
    return get_db_settings().auth_database_url is not None


def _warn_enforcement_disabled_once() -> None:
    """Say loudly, once, that no session is being required.

    The stand-down below is deliberate (pre-auth deployment posture), but it was originally
    SILENT, and a silent one is indistinguishable from the failure it most resembles: a typo in
    backend/.env, or an env_file that did not load. In that state every route serves without a
    session and nothing anywhere says so — the dashboard looks entirely normal.

    SENIOR_ENGINEER_BAR §7.2 requires a guardrail to announce when it blocks. A guardrail that has
    stopped guarding deserves at least the same. Logged once per process rather than per request,
    so it is visible at startup without drowning the log.
    """
    global _enforcement_warning_emitted
    if _enforcement_warning_emitted:
        return
    _enforcement_warning_emitted = True
    logger.warning(
        "AUTH ENFORCEMENT DISABLED: AUTH_DATABASE_URL is not configured, so no operator session "
        "is required on any route. This is the expected pre-auth posture and the Caddy basic-auth "
        "outer gate is the only control. If you did NOT intend this, backend/.env is missing or "
        "did not load — set AUTH_DATABASE_URL and restart."
    )


def enforce_authenticated(request: Request) -> None:
    """Require a valid operator session on every route outside the explicit allow-list.

    Allow-list: ``/api/health`` and the auth routes themselves — nothing else (§4). The CSRF
    guard (``enforce_same_origin``) runs first in main.py's dependency list; the two compose, and
    neither replaces the other (§5.9).

    When ``AUTH_DATABASE_URL`` is NOT configured, this dependency stands down: that is the
    pre-auth deployment posture (no operators can exist — they live in the database that isn't
    there), under which the dashboard has always served behind the Caddy basic-auth outer gate
    (§5.13) and must keep doing so (db/config.py: "no database" is a supported first-class
    state). This is a deployment-presence switch, not a graceful degradation: the moment the DSN
    is configured, every failure of the auth store is a REFUSAL — a configured-but-unreachable
    database answers 503 via :class:`AuthUnavailable` (never a default-allow, §8/§11.6), and a
    missing or invalid cookie answers 401. Prod compose sets AUTH_DATABASE_URL; unsetting it
    requires host access, which is total compromise independently (§6).
    """
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith(_AUTH_ROUTE_PREFIX):
        return
    if get_db_settings().auth_database_url is None:
        _warn_enforcement_disabled_once()
        return
    token = request.cookies.get(SESSION_COOKIE_NAME)
    # Idle renewal (§5.3): only NON-safe methods count as operator activity here. The dashboard's
    # SWR timers re-GET data endpoints every 10-30 s, so a safe-method touch would renew the idle
    # window forever on an unattended machine. Deliberate operator actions are POSTs (refresh,
    # debate, logout), and the /api/auth/me revalidation the shell fires on real window-focus
    # events goes through the auth router (allow-listed above), which touches unconditionally —
    # so a present human keeps their session alive; an abandoned tab does not.
    touch = request.method not in ("GET", "HEAD", "OPTIONS")
    operator = authenticate_session(token, touch=touch) if token else None
    if operator is None:
        raise HTTPException(status_code=401, detail="authentication required")
    request.state.operator = operator
