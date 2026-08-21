"""FastAPI application entrypoint for the agentic dashboard backend.

Wires logging (with secret redaction), CORS, the CSRF guard, ensures the mounted data/logs
directories exist, and mounts the API routers. ``health`` is always present.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    account,
    auth,
    data_trust,
    debate,
    fundamentals,
    health,
    history,
    market_context,
    performance,
    pipeline,
    position,
    reconciliation,
    scan,
)
from app.routers import (
    accounts as accounts_router,
)
from app.routers import (
    chat as chat_router,
)
from app.routers import (
    cycle as cycle_router,
)
from app.routers import (
    settings as settings_router,
)
from app.services.auth import auth_enforcement_configured, enforce_authenticated
from app.services.email import assert_production_transport


class SecretRedactionFilter(logging.Filter):
    """Scrub API-key / auth-header material from log records before a handler emits them.

    Defends against: secret leakage into log files (OWASP A09). No current code path logs the
    Anthropic key on purpose, but an SDK exception that embeds an ``Authorization`` header (or the
    key itself) would otherwise land verbatim in ``logger.exception`` output and persist in
    ``logs/cron/``. Must be attached to *handlers*, not loggers — logger-level filters do not apply
    to records propagated up from child loggers, handler-level filters see every record they emit.
    """

    # (pattern, replacement) applied in order. The sk-ant rule runs first so a key inside an
    # Authorization/bearer value is destroyed even before the surrounding header is masked; the
    # otpauth rule likewise runs before the base32 rule so a provisioning URI is destroyed
    # wholesale (label, issuer, and secret= parameter together), with the base32 rule as the
    # second net for a bare secret outside a URI.
    _RULES: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-[REDACTED]"),
        # AUTH_THREAT_MODEL §5.4: an otpauth:// URI embeds the TOTP shared secret as a query
        # parameter — one log line and the second factor is reproducible forever. The whole URI
        # goes, not just the parameter.
        (re.compile(r"otpauth://\S+"), "otpauth://[REDACTED]"),
        # §5.4: bare base32 secret material — 16+ chars of the RFC 4648 alphabet (A-Z, 2-7,
        # optional padding), the shape of every TOTP secret. Runs that long of ONLY these
        # characters are essentially never legitimate log content (\b keeps it from firing
        # inside mixed-case identifiers), and a false positive is hygiene-safe: an over-redacted
        # log line beats a leaked second factor.
        #
        # DELIBERATELY NOT TIGHTENED. This also eats bare 16+ letter all-caps words
        # ("MISCONFIGURATION"), and the obvious narrowing is to require a digit in the run:
        # `(?=[A-Z2-7]*[2-7])`. Do not. pyotp draws each of a secret's 32 characters uniformly
        # from the 32-symbol alphabet, so P(no 2-7 anywhere) = (26/32)**32 ≈ 0.0016 — about one
        # secret in 600 would then log in the clear, and the one that escapes is not knowable in
        # advance. Losing an uppercase word from a log line costs nothing; a 1-in-600 chance of
        # writing a permanent second factor to disk is not a trade worth making for legibility.
        # A re-review measured the realistic auth log lines (startup warnings, CSRF blocks,
        # rate-limit hits, psycopg tracebacks, constraint names, container ids, UUIDs) and found
        # every one survives unchanged — underscores, digits and mixed case break the run.
        (re.compile(r"\b[A-Z2-7]{16,}(?:=+)?"), "[REDACTED-BASE32]"),
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*"), "Bearer [REDACTED]"),
        (
            re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|api[_-]?key)\b\s*[:=]\s*\S+"),
            r"\1: [REDACTED]",
        ),
    )
    _FORMATTER = logging.Formatter()

    @classmethod
    def _redact(cls, text: str) -> str:
        for pattern, replacement in cls._RULES:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info or record.exc_text:
            # Pre-render the traceback and scrub it; Formatter.format() reuses a populated
            # ``exc_text`` instead of re-rendering ``exc_info``, so the scrubbed text is what
            # lands. If another handler already formatted (and cached) the traceback before this
            # filter ran, scrub the cached text rather than trusting it.
            if not record.exc_text and record.exc_info:
                record.exc_text = self._FORMATTER.formatException(record.exc_info)
            record.exc_text = self._redact(record.exc_text or "") or None
        return True  # always redact-and-pass, never drop — a swallowed record would hide the event


def configure_logging() -> None:
    """Process-wide logging bootstrap shared by the API and the cycle job. Idempotent.

    ``basicConfig`` is a no-op when the root logger already has handlers, and the filter is only
    added to handlers that don't already carry one, so repeated calls don't stack duplicates.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())


configure_logging()
logger = logging.getLogger("agentic.api")


# --- CSRF guard (issue #11) -----------------------------------------------------------------
# Methods that never change state here; everything else must pass the same-origin checks.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Sec-Fetch-Site values for requests originating from this site (or user-initiated: "none").
# "same-site" is required because the dev frontend runs on a different localhost port.
_TRUSTED_FETCH_SITES = frozenset({"same-origin", "same-site", "none"})


def _origin_allowed(origin: str) -> bool:
    """True if ``origin`` is in the CORS allow-list (exact entries or the localhost regex)."""
    settings = get_settings()
    if origin in settings.cors_origin_list:
        return True
    regex = settings.cors_origin_regex_or_none
    # fullmatch, not match: with a prefix match "http://localhost:3000.evil.example" would pass.
    return bool(regex and re.fullmatch(regex, origin))


def _blocked(request: Request, reason: str, detail: str) -> HTTPException:
    # Guardrails never block silently: name the block, the reason, and the offending values.
    logger.warning(
        "CSRF guard BLOCKED %s %s — %s | origin=%r sec-fetch-site=%r content-type=%r client=%s",
        request.method,
        request.url.path,
        reason,
        request.headers.get("origin"),
        request.headers.get("sec-fetch-site"),
        request.headers.get("content-type"),
        request.client.host if request.client else "unknown",
    )
    return HTTPException(status_code=403, detail=detail)


async def enforce_same_origin(request: Request) -> None:
    """Same-origin + content-type guard for every state-changing route.

    Defends against: CSRF. The dashboard sits behind ambient browser credentials (Caddy basic
    auth in the shared deploy), and CORS only blocks *reading* a cross-site response — a form or
    top-level POST is still *sent*, credentials attached. Today the state-changing routes are the
    journal writes and the debate/scan/pipeline runs; once an order path exists the same request
    shape places a trade. Checks, in order:

    1. Body content type must be ``application/json`` — an HTML form can only submit the three
       CORS-"simple" form types, so this alone kills the auto-submitting-form vector, and every
       legitimate client here already sends JSON.
    2. If the browser sent ``Sec-Fetch-Site`` (a forbidden header — scripts cannot forge it), it
       must be same-origin/same-site/none.
    3. Otherwise, if an ``Origin`` header is present it must match the CORS allow-list
       (``null`` — sandboxed iframes, file:// — matches nothing and is rejected).
    4. No Sec-Fetch-Site and no Origin means a non-browser client (curl, tests, monitoring):
       allowed, because CSRF needs a victim browser to attach ambient credentials.

    Registered app-wide via ``FastAPI(dependencies=...)`` in :func:`create_app`, so a newly added
    router is covered without having to remember anything.
    """
    if request.method in _SAFE_METHODS:
        return

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _blocked(
            request,
            f"content type {content_type or '(none)'!r} is not application/json",
            "State-changing requests must send Content-Type: application/json.",
        )

    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site:
        if fetch_site not in _TRUSTED_FETCH_SITES:
            raise _blocked(
                request,
                f"sec-fetch-site {fetch_site!r} is not same-origin",
                "Cross-site requests to this API are not allowed.",
            )
        return

    origin = request.headers.get("origin")
    if origin is not None and not _origin_allowed(origin):
        raise _blocked(
            request,
            f"origin {origin!r} is not in the CORS allow-list",
            "Cross-origin requests to this API are not allowed.",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Make sure the volume-mounted dirs exist before any request touches them."""
    settings = get_settings()
    for path in (settings.data_dir, settings.logs_dir, settings.debates_dir):
        path.mkdir(parents=True, exist_ok=True)
    logger.info(
        "backend up | account=%s snapshot_present=%s debate_ready=%s",
        settings.agentic_account_masked,
        settings.snapshot_path.exists(),
        settings.anthropic_api_key is not None,
    )

    # §5.6 mail-transport posture. email.py ships assert_production_transport() as a guard against
    # booting the prod profile on the log-only mock — which logs full message bodies, verification
    # links included. Nothing called it: it landed between two changes' scopes, so the guard existed
    # and never ran.
    #
    # The trigger is auth being configured. Once operators exist, a verification link is a bearer
    # credential, and a transport that writes it to the log is a disclosure rather than a
    # convenience. Warned rather than raised because this app has no explicit prod-profile signal,
    # and refusing to boot the whole dashboard on a mail misconfiguration is a bigger outage than
    # the fault. Promoting it to a hard failure is the right end state and needs that signal first.
    if auth_enforcement_configured():
        try:
            assert_production_transport()
        except RuntimeError as exc:
            logger.error("MAIL TRANSPORT UNSAFE FOR AUTH: %s", exc)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Agentic Robinhood Dashboard",
        description="Read-only account monitor + live Sprinkle Sauce screen + jury debate engine.",
        version="0.1.0",
        lifespan=lifespan,
        # AUTH_THREAT_MODEL §4: the session allow-list is /api/health + /api/auth/* and NOTHING
        # else — and that must include FastAPI's own bootstrap surface. /openapi.json, /docs,
        # /redoc, and /docs/oauth2-redirect are registered by FastAPI.setup() as plain Starlette
        # Routes, not APIRoutes, so the app-wide `dependencies=[...]` below NEVER runs for them:
        # with auth configured, the full API schema was being served unauthenticated.
        # Disabled outright rather than allow-listed — this is a two-operator
        # private dashboard whose API surface is documented in the code, not a public API.
        # test_auth_routes.py enumerates every reachable route to pin the allow-list closed.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        # App-wide so every route — current and future — passes both guards without anyone
        # remembering to add them (SECURITY.md §6 invariant 3; AUTH_THREAT_MODEL §4). Order
        # matters: the CSRF guard runs first, then the session gate. enforce_authenticated
        # allow-lists /api/health and the auth routes themselves, and nothing else; when
        # AUTH_DATABASE_URL is configured but unreachable it REFUSES with 503 (fail closed,
        # AUTH_THREAT_MODEL §8 / §11.6), never a default-allow.
        dependencies=[Depends(enforce_same_origin), Depends(enforce_authenticated)],
    )

    # Default: allow localhost/127.0.0.1 on any port (the frontend port is random) via regex, plus any
    # explicit origins from CORS_ORIGINS. We never default to "*" — this backend fronts a live
    # brokerage account and a billable API key.
    # allow_credentials stays False, so the permissive localhost regex carries no cookie/auth risk.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex_or_none,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(data_trust.router)
    app.include_router(auth.router)
    app.include_router(chat_router.router)
    app.include_router(cycle_router.router)
    app.include_router(account.router)
    app.include_router(accounts_router.router)
    app.include_router(scan.router)
    app.include_router(settings_router.router)
    app.include_router(debate.router)
    app.include_router(pipeline.router)
    app.include_router(history.router)
    app.include_router(market_context.router)
    app.include_router(fundamentals.router)
    app.include_router(performance.router)
    app.include_router(position.router)
    app.include_router(reconciliation.router)
    return app


app = create_app()
