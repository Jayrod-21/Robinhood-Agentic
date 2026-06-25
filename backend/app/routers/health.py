"""Health + readiness endpoint, used by the Docker healthcheck and the frontend banner."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings

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
    }
