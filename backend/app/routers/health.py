"""Health + readiness endpoint, used by the Docker healthcheck and the frontend banner."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.services.auth import auth_enforcement_configured
from app.services.broker import alpaca_configured

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness + a small readiness summary (never leaks secret values).

    ``debate_ready`` reflects only whether a key is *present*, never the key itself.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "service": "agentic-dashboard-backend",
        # WHICH account this instance is actually reporting on.
        #
        # This used to render settings.agentic_account_masked unconditionally — the Robinhood
        # number, baked into config. The moment the account of record became Alpaca, a live
        # endpoint began naming an account the dashboard no longer reads: a masked number that
        # looked authoritative and was wrong. A probe answering with the wrong account is worse
        # than one that omits it.
        #
        # It now names the source rather than a number, because the number is only knowable by
        # calling the broker, and a liveness probe must not depend on an upstream API. "alpaca"
        # here means "this instance is configured to read Alpaca"; the actual masked number, from
        # the broker itself, is on /api/account and /api/data-trust.
        "account_source": "alpaca" if alpaca_configured() else "snapshot-file",
        "snapshot_present": settings.snapshot_path.exists(),
        "debate_ready": settings.anthropic_api_key is not None,
        "jury_model": settings.jury_model,
        "synth_model": settings.synth_model,
        # Whether an operator session is REQUIRED on protected routes. False is the legitimate
        # pre-auth posture (AUTH_DATABASE_URL unset, no operators can exist) — but it is also what
        # a mislaid backend/.env looks like, and in that state every route serves without a
        # session behind only the Caddy outer gate. Reported here so the posture is observable
        # rather than inferable from behaviour; the service also warns once at first use.
        # Boolean only — never the DSN.
        "auth_enforced": auth_enforcement_configured(),
    }
