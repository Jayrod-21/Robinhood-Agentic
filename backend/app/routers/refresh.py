"""POST /api/refresh — queue a real account refresh for the host-side daemon.

The container cannot reach the Robinhood MCP (it's an OAuth server scoped to this Claude session on
the host). So "refresh" is a bridge: this endpoint drops a trigger file on the shared volume, and
``bin/refresh_daemon.sh`` — running on the host, outside Docker — picks it up, pops a terminal/VS
Code tab running ``claude`` (which has the MCP), rewrites the snapshot, and removes the trigger.

The file this refreshes is now the FALLBACK account source: when Alpaca credentials are configured,
``/api/account`` reads the broker live (services/broker.py) and never touches the snapshot file, so
this endpoint only matters on the Robinhood-file path.

We only signal intent here; we never touch Robinhood credentials. A short cooldown stops a mashed
button from queuing a tab storm.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.services.broker import alpaca_configured
from app.services.snapshot import SnapshotError, load_snapshot

router = APIRouter(prefix="/api", tags=["refresh"])

# Monotonic timestamp of the last honored request, for cooldown enforcement (process-local).
# Guarded by ``_request_lock`` so the exists-check / cooldown-check / write / stamp sequence is
# atomic: FastAPI dispatches this sync endpoint to a threadpool, so two concurrent clicks could
# otherwise both pass the gate (TOCTOU) and queue two tabs. The lock collapses that window.
_last_request_monotonic: float | None = None
_request_lock = threading.Lock()


class RefreshResponse(BaseModel):
    status: str  # "queued" | "pending" | "cooldown"
    detail: str
    requested_at: str | None = None
    cooldown_remaining_s: int = 0


class RefreshStatus(BaseModel):
    pending: bool
    snapshot_generated_at: str | None
    cooldown_remaining_s: int


def _snapshot_generated_at() -> str | None:
    """When the FALLBACK snapshot file was written — or None when it is not the live source.

    This used to load the file unconditionally, which meant that with Alpaca serving the account
    view the status panel reported the age of a file nothing was reading: a weeks-old timestamp
    displayed beside live holdings, under a label saying when the data was generated. The number
    was real; what it described was not the thing on screen.

    With Alpaca configured this returns None, because the file's age is not a fact about the
    displayed account. The account's own freshness is on /api/data-trust and /api/account, which
    read it from the source actually being served.
    """
    if alpaca_configured():
        return None
    settings = get_settings()
    try:
        return load_snapshot(settings.snapshot_path).generated_at
    except SnapshotError:
        return None


def _atomic_write(path, contents: str) -> None:
    """Write ``contents`` to ``path`` atomically (temp file in the same dir + os.replace).

    The host daemon polls for ``refresh.request`` and parses it; a non-atomic write would let it
    observe a zero-length or partial file. ``os.replace`` is an atomic rename on the same filesystem,
    so the daemon only ever sees a complete file (or no file).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(contents)
        os.replace(tmp, path)
    except OSError:
        # Best-effort cleanup of the temp file; re-raise so the caller can surface a 503.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@router.post("/refresh", response_model=RefreshResponse)
def request_refresh() -> RefreshResponse:
    global _last_request_monotonic
    settings = get_settings()
    req_path = settings.refresh_request_path

    # Whole check-then-write sequence under the lock so two concurrent requests can't both pass the
    # gate and queue two tabs (TOCTOU). The work inside is trivial (a small file write).
    with _request_lock:
        now = time.monotonic()

        # If a trigger is already waiting, the daemon just hasn't consumed it yet.
        if req_path.exists():
            return RefreshResponse(
                status="pending",
                detail="A refresh is already queued and waiting for the host daemon.",
            )

        # Cooldown so rapid clicks don't spawn a stack of terminal tabs.
        if _last_request_monotonic is not None:
            elapsed = now - _last_request_monotonic
            if elapsed < settings.refresh_cooldown_seconds:
                remaining = int(settings.refresh_cooldown_seconds - elapsed) + 1
                return RefreshResponse(
                    status="cooldown",
                    detail=f"Refresh cooling down; try again in ~{remaining}s.",
                    cooldown_remaining_s=remaining,
                )

        requested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = json.dumps(
            {"requested_at": requested_at, "account": settings.agentic_account_masked}
        ) + "\n"
        try:
            _atomic_write(req_path, payload)
        except OSError as exc:
            # e.g. a read-only bind mount — fail with a clear 503 rather than an unhandled 500.
            raise HTTPException(
                status_code=503,
                detail="Could not queue refresh (data volume not writable).",
            ) from exc
        _last_request_monotonic = now

    return RefreshResponse(
        status="queued",
        detail="Refresh queued. The host daemon will rewrite the Robinhood snapshot file "
        "(the fallback account source) via the Robinhood MCP.",
        requested_at=requested_at,
    )


@router.get("/refresh/status", response_model=RefreshStatus)
def refresh_status() -> RefreshStatus:
    settings = get_settings()
    remaining = 0
    if _last_request_monotonic is not None:
        elapsed = time.monotonic() - _last_request_monotonic
        if elapsed < settings.refresh_cooldown_seconds:
            remaining = int(settings.refresh_cooldown_seconds - elapsed) + 1
    return RefreshStatus(
        pending=settings.refresh_request_path.exists(),
        snapshot_generated_at=_snapshot_generated_at(),
        cooldown_remaining_s=remaining,
    )
