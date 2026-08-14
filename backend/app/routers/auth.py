"""Authentication routes — the HTTP surface of docs/AUTH_THREAT_MODEL.md §4.

The wire contract here is pinned by the already-built frontend (frontend/src/lib/auth.ts):

  * ``POST /api/auth/login``        → 200 ``{status:"mfa_required", challenge_token, expires_in}``
                                      on success; every failure is the IDENTICAL-shape 200 with
                                      one fixed body (§5.2 — unknown address and wrong password
                                      must be indistinguishable in status, body, and timing).
  * ``POST /api/auth/login/totp``   → 204 + ``Set-Cookie: __Host-rh_sid=…`` (the ONLY place a
                                      session is minted); 401 on a rejected second factor;
                                      423 + ``retry_after`` under the §5.8 lockout.
  * ``POST /api/auth/logout``       → 204, server-side revocation.
  * ``GET  /api/auth/me``           → ``{email, email_verified}`` or 401.
  * ``POST /api/auth/verify``       → 200 ``{status: verified|already_verified}``; specific 4xx
                                      reasons (this flow sits after the email round-trip, §5.6).
  * ``POST /api/auth/verify/resend``→ 202 always — no enumeration (§5.2).

There is deliberately NO ``/reset`` and NO ``/forgot-password`` (§5.7): recovery is the host CLI
(``bin/manage_operator.py``), because an emailed reset would make mailbox access equal account
access. A test pins the absence.

These routes are covered by BOTH app-wide dependencies from main.py: ``enforce_same_origin``
(CSRF, §5.9 — they are state-changing JSON POSTs like everything else) and
``enforce_authenticated`` (which allow-lists them, since they are how a session is obtained).
On top of those, the router carries its own third gate: ``enforce_auth_cooldown`` (§5.1/§3.3),
a route-wide rolling-window rate limit answering 423 + ``retry_after`` when tripped.
Database failures surface as :class:`app.services.auth.AuthUnavailable` — a 503 refusal, never a
default-allow (§8).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.ratelimit import KeyedWindowLimiter, WindowLimiter
from app.services import auth as auth_service
from app.services.auth import SESSION_COOKIE_NAME, client_ip
from app.services.email import EmailDeliveryError, send_verification_email

logger = logging.getLogger("agentic.auth.routes")

# --- §5.1/§3.3 route-wide cooldown -----------------------------------------------------------
# One in-process, lock-guarded budget over every credential-processing /api/auth/* request. This
# is the compensating control for the deliberate §5.8 design choice that the PASSWORD step has no
# per-account counter (the lockout counter lives on operator_totp and only the TOTP step bumps
# it). Without this gate the password step is (a) unbounded online guessing and (b) a
# memory-exhaustion vector: ``login`` is a sync handler run on FastAPI's threadpool, and every
# request burns a 64 MiB Argon2id verification — a saturated 40-worker pool is ~2.5 GB against a
# container with no memory limit.
#
# Tuning lives in module constants (the scan.py::SCAN_MIN_INTERVAL_SECONDS precedent): 12 requests
# per rolling 60 s, route-wide. A full legitimate login is 2 POSTs (password + TOTP); even a
# couple of typo'd retries plus a logout stay comfortably inside the budget, while an attacker
# gets at most 12 password guesses and ~768 MiB of transient Argon2 work per minute — layered ON
# TOP of the per-operator §5.8 lockout (which is untouched by this gate), never instead of it.
AUTH_RATE_MAX_REQUESTS = 12
AUTH_RATE_WINDOW_SECONDS = 60.0

# The GLOBAL ceiling, across every client. Higher than the per-client budget on purpose: its job is
# to bound total Argon2 work (60 x ~130 ms ~= 8 s of CPU per minute, transient 64 MiB each), not to
# decide who gets refused. Deciding that is the per-client gate's job, and conflating the two is
# what made a single shared budget into a denial-of-service against the operators (§5.13).
AUTH_RATE_GLOBAL_MAX_REQUESTS = 60

# Cloudflare sets CF-Connecting-IP at the edge to the true client address and OVERWRITES whatever
# the client sent, so it cannot be forged by a remote caller travelling the only route in.
_CLIENT_IP_HEADER = "cf-connecting-ip"


def rate_limit_key(request: Request) -> str:
    """The per-client key for the auth gate — NOT the value written to the audit log.

    Deliberately separate from services.auth.client_ip, which stays on the direct peer address and
    refuses forwarded headers because a forged value there would poison the audit trail. These two
    want different things: the audit log wants what it can PROVE, the rate limiter wants who the
    caller most likely IS, and it fails safe when it guesses wrong (a wrong key refuses that key,
    it does not admit anyone).

    WHY THE HEADER IS TRUSTABLE HERE, AND THE INVARIANT THAT KEEPS IT TRUE
        The backend publishes no host port (deploy/docker-compose.prod.yml: `expose`, never
        `ports`), so the only route to it is cloudflared -> Caddy -> here, and Cloudflare rewrites
        CF-Connecting-IP at the edge. **If that ever changes — a published port, a second ingress,
        a proxy that forwards the header unchanged — this header becomes attacker-chosen and this
        function must stop trusting it.** The damage would be bounded rather than total: a caller
        rotating the header evades only their own per-client budget and still meets the global
        ceiling, which is precisely why the ceiling is not optional.

    Falls back to the peer address (dev stack, tests, local curl), then to a fixed key so an
    unidentifiable caller shares one budget rather than escaping the gate entirely.
    """
    forwarded = request.headers.get(_CLIENT_IP_HEADER, "").strip()
    if forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))  # normalised; garbage never becomes a key
        except ValueError:
            pass  # malformed header: ignore it and fall through, never trust it verbatim
    peer = request.client.host if request.client else None
    return peer or "unidentified"


# Safe methods are exempt on purpose, documented deviation from a literal "all /api/auth/*
# routes": the cost this gate guards is credential-verification work, and the only GET here
# (/api/auth/me) does none — it is the frontend's sole source of auth state, called on every page
# load. Metering it would let ordinary page traffic (or an attacker) starve the budget so that
# fetchMe() fails and the UI silently renders the operator logged out — exactly the silent-block
# failure mode the guardrail rules prohibit. Hammering GET /me is no cheaper than hammering any
# other session-gated GET on the app, none of which are (or need to be) inside this budget.
_RATE_EXEMPT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_limiter_init_lock = threading.Lock()


def _per_client_limiter_for(request: Request) -> KeyedWindowLimiter:
    """The per-client gate, app-instance-scoped for the same reason as the global one below."""
    limiter = getattr(request.app.state, "auth_client_rate_limiter", None)
    if limiter is None:
        with _limiter_init_lock:
            limiter = getattr(request.app.state, "auth_client_rate_limiter", None)
            if limiter is None:
                limiter = KeyedWindowLimiter()
                request.app.state.auth_client_rate_limiter = limiter
    return limiter


def _rate_limiter_for(request: Request) -> WindowLimiter:
    """The app-instance-scoped limiter, created on first use (double-checked under a lock).

    Deliberately NOT a module global like ``debate_limiter``: that one is global because two
    routers must draw from a single spend budget. Here exactly one router draws from the gate,
    and a module global would couple every app instance in the process — every TestClient app in
    a pytest run — to one shared budget. Production runs one app per process, so per-app is
    exactly the single in-process budget §3.3 specifies.
    """
    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if limiter is None:
        with _limiter_init_lock:
            limiter = getattr(request.app.state, "auth_rate_limiter", None)
            if limiter is None:
                limiter = WindowLimiter()
                request.app.state.auth_rate_limiter = limiter
    return limiter


def enforce_auth_cooldown(request: Request) -> None:
    """§5.1 route-wide cooldown. Registered as a ROUTER dependency below, so every /api/auth/*
    route — current and future — is covered without per-route wiring. It runs after main.py's
    app-wide CSRF + session dependencies (so cross-site garbage dies before it can consume the
    budget) and before the handler and its body validation, so a blocked request never reaches
    Argon2, the TOTP verifier, or the database."""
    if request.method in _RATE_EXEMPT_METHODS:
        return
    # Per-client FIRST: a caller over their own budget must be refused without spending a slot of
    # the global ceiling, or a single hammering client would still starve everyone else — the very
    # denial-of-service this gate was split in two to fix.
    key = rate_limit_key(request)
    wait = _per_client_limiter_for(request).check_and_consume(
        key, AUTH_RATE_MAX_REQUESTS, AUTH_RATE_WINDOW_SECONDS
    )
    scope = "this client"
    if not wait:
        wait = _rate_limiter_for(request).check_and_consume(
            AUTH_RATE_GLOBAL_MAX_REQUESTS, AUTH_RATE_WINDOW_SECONDS
        )
        scope = "all clients"
    if not wait:
        return
    # Guardrails never block silently: name the gate, the reason, and the caller in the server
    # log, and put the honest wait in the response body.
    # The scope is in the message because the two gates mean very different things operationally:
    # "this client" is one caller hitting their own ceiling (routine), "all clients" means the
    # global cap is saturated — i.e. either a distributed attempt or a budget that needs raising,
    # and the operators may be unable to sign in. Those must not look identical in the log.
    logger.warning(
        "auth rate limit hit (%s): %s %s from %s [key=%s] blocked for ~%ss — over %s requests in "
        "the last %ss across /api/auth/* (§5.1 gate against credential guessing and Argon2 memory "
        "exhaustion; per-operator §5.8 lockout state is unaffected)",
        scope,
        request.method,
        request.url.path,
        client_ip(request),
        key,
        wait,
        AUTH_RATE_MAX_REQUESTS if scope == "this client" else AUTH_RATE_GLOBAL_MAX_REQUESTS,
        int(AUTH_RATE_WINDOW_SECONDS),
    )
    # 423 + retry_after, NOT 429: the built frontend (frontend/src/lib/auth.ts) maps 423 with a
    # retry_after (top-level or under `detail`) to an honest "locked, retry in N s" state, while
    # an unrecognized 429 would collapse into `rejected` and render as "invalid credentials" — a
    # lie about what happened. This is the ROUTE-wide gate; the per-operator §5.8 lockout keeps
    # its own, independent 423 in login_totp and is not weakened or replaced by this one.
    raise HTTPException(
        status_code=423,
        detail={
            "status": "locked",
            "retry_after": wait,
            "reason": "too many authentication requests; wait and retry",
        },
    )


router = APIRouter(
    prefix="/api/auth", tags=["auth"], dependencies=[Depends(enforce_auth_cooldown)]
)

# §5.2: ONE body for every password-step failure — same status, same bytes, no error taxonomy.
_LOGIN_FAILURE_BODY = {"status": "invalid_credentials"}


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent") or None


def _set_session_cookie(response: Response, token: str) -> None:
    """The one place the session cookie is written; test_auth_cookie.py pins every attribute.

    ``secure=True`` is a CONSTANT — never derived from request.url.scheme or X-Forwarded-Proto,
    which an attacker may control if the origin is ever reachable directly (§5.3). Browsers treat
    localhost as a secure context, so this works in the dev stack too. No ``domain=`` argument,
    ever: the __Host- prefix (see SESSION_COOKIE_NAME) makes the browser reject the cookie if one
    appears, which is the defense against sibling-subdomain cookie tossing.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=get_settings().auth_session_ttl_hours * 3600,  # hint only; expiry is server-side
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


class LoginRequest(BaseModel):
    # max_length bounds request abuse only; the real 256-byte password cap (§5.1) lives in the
    # service, where oversize input takes the dummy-verify path instead of a distinguishable 422.
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=4096)


@router.post("/login")
def login(req: LoginRequest, request: Request) -> dict[str, Any]:
    """Step 1: password → challenge. NEVER mints a session and never sets a cookie (§4)."""
    issued = auth_service.password_login(
        req.email, req.password, ip=client_ip(request), user_agent=_user_agent(request)
    )
    if issued is None:
        return dict(_LOGIN_FAILURE_BODY)
    return {
        "status": "mfa_required",
        "challenge_token": issued.token,
        "expires_in": issued.expires_in,
    }


class TotpRequest(BaseModel):
    challenge_token: str = Field(min_length=1, max_length=256)
    # TOTP digits OR a recovery code, one field — the recovery path shares this route precisely
    # so it cannot accidentally have weaker rate limiting or lockout than TOTP (§5.5).
    code: str = Field(min_length=1, max_length=64)


@router.post("/login/totp", status_code=204)
def login_totp(req: TotpRequest, request: Request) -> Response:
    """Step 2: challenge + code → 204 with the session cookie; 401 rejected; 423 locked."""
    try:
        token = auth_service.complete_mfa(
            req.challenge_token, req.code, ip=client_ip(request), user_agent=_user_agent(request)
        )
    except auth_service.AccountLocked as exc:
        # §5.8: observable, never silent — the remaining wait and a plain reason, in the body.
        return JSONResponse(
            status_code=423,
            content={
                "status": "locked",
                "retry_after": exc.retry_after,
                "reason": "too many failed second-factor attempts",
            },
        )
    except auth_service.SecondFactorRejected:
        raise HTTPException(status_code=401, detail="second factor rejected") from None
    response = Response(status_code=204)
    _set_session_cookie(response, token)
    return response


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    """Server-side revocation (§5.3). Idempotent 204; clearing the cookie alone is not a logout,
    so a database failure here is a 503, not a silently-cleared cookie over a live session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth_service.revoke_session(
            token, ip=client_ip(request), user_agent=_user_agent(request)
        )
    response = Response(status_code=204)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
    )
    return response


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    """The only source of client-side auth state (the cookie is HttpOnly by design)."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    operator = auth_service.authenticate_session(token) if token else None
    if operator is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return {"email": operator.email, "email_verified": operator.email_verified}


class VerifyRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post("/verify")
def verify(req: VerifyRequest) -> dict[str, Any]:
    """Redeem an email-verification token (POSTed by the /verify-email page, which read it from
    the URL fragment — the server never sees the token in a URL, §5.6). Confers no session."""
    try:
        status = auth_service.consume_verification_token(req.token)
    except auth_service.VerificationRejected as exc:
        # Specific reasons are safe and helpful here: this flow sits AFTER the email round-trip,
        # so §5.2's enumeration constraint does not apply (the frontend maps these strings).
        raise HTTPException(status_code=400, detail=exc.reason) from None
    return {"status": status}


class ResendRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


@router.post("/verify/resend", status_code=202)
async def verify_resend(req: ResendRequest) -> dict[str, Any]:
    """Always 202 with one fixed body — unknown address, already verified, and cooldown-suppressed
    are indistinguishable from a send (§5.2/§5.6). The cooldown probe and the token insert are
    atomic in the service; only the SMTP hop happens after commit, and its failure is logged
    server-side without changing the response."""
    decision = await asyncio.to_thread(auth_service.request_verification_resend, req.email)
    if decision.send and decision.email and decision.token:
        try:
            await send_verification_email(
                decision.email, decision.token, ttl_hours=decision.ttl_hours
            )
        except EmailDeliveryError:
            # Constant-message error by contract; the real cause was already logged (scrubbed)
            # by the transport. The 202 stands — suppression must stay invisible.
            logger.warning("verification resend delivery failed (response unchanged)")
    return {"status": "accepted"}
