"""Health + readiness endpoint, used by the Docker healthcheck and the frontend banner."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.services.auth import auth_enforcement_configured

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
        "account": settings.agentic_account_masked,
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
