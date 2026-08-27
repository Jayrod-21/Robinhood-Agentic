"""GET /api/cycle/current — is the scheduled cycle running, and how far in?

Contract: docs/contracts/cycle-endpoint.md. Read-only.

WHY POLLING AND NOT A STREAM
    The cycle is a different PROCESS from the API (bin/scheduled_cycle.sh runs it through
    `docker compose exec`), so its events cannot reach a stream this app is serving. Progress goes
    through the database, and the page reads it on an interval. For a job that takes twenty minutes
    end to end, a ten-second poll loses nothing a stream would have given.

WHAT AN EMPTY ANSWER MEANS
    `run: null` means no cycle has ever been recorded, which is a different statement from "no
    cycle is running now" — a deployment that has never run one and a quiet Sunday look identical
    otherwise, and only one of those is worth investigating.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db import DbUnavailable
from app.services import cycle_state

router = APIRouter(prefix="/api", tags=["cycle"])


def _unavailable(exc: DbUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=f"The database is unavailable: {exc}")


@router.get("/cycle/current")
def current_cycle() -> dict[str, Any]:
    try:
        run = cycle_state.current()
        history = cycle_state.recent(limit=10)
    except DbUnavailable as exc:
        raise _unavailable(exc) from None

    return {
        "meta": {
            # Stated so the page can size its own interval off the source rather than guessing, and
            # so "why is this stale" has an answer on the payload.
            "poll_seconds": 10,
            "stale_after_minutes": cycle_state.STALE_AFTER_MINUTES,
            # Distinguishes "nothing has ever run" from "nothing is running".
            "has_ever_run": run is not None,
            "is_running": bool(run and run["status"] == "running"),
        },
        "run": run,
        "recent": history,
    }


@router.get("/cycle/runs")
def cycle_runs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """The run history — what ran, when, how long it took, and what failed."""
    try:
        return {"runs": cycle_state.recent(limit=limit)}
    except DbUnavailable as exc:
        raise _unavailable(exc) from None
