"""The backend's authenticated door to the Testing Lab.

WHY THIS EXISTS AT ALL
    The Lab is a separate container (lab/), and it authenticates nobody. That is deliberate and it
    is safe only because of one fact: the Lab sits on `rh-internal` with no host port and no Caddy
    route, so the ONLY way to reach it is from inside this process. Everything arriving here has
    already passed the app-wide CSRF guard and session gate registered in app/main.py, because this
    is an APIRouter and those are app-wide dependencies.

    So the security boundary is this file plus the compose file. If a `ports:` line or a Caddy route
    is ever added to the lab service, the Lab becomes an unauthenticated endpoint that trains models
    on demand. That warning is repeated in lab/app.py and in the compose service comment.

WHY AN ALLOW-LIST AND NOT A PASSTHROUGH
    A `{path:path}` proxy forwards whatever the Lab happens to expose, including routes added later
    that nobody reviewed at this boundary. The routes below are enumerated, so a new Lab endpoint is
    unreachable from the internet until someone adds it here on purpose.

WHY THE OPERATOR IS OVERWRITTEN, NEVER FORWARDED
    The Lab records an `operator` on every experiment. If this proxy passed the client's value
    through, that column would record a claim rather than an identity — the same rule
    routers/settings.py follows for every audited write. The body's `operator` field is replaced
    with request.state.operator before the request leaves this process.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.ratelimit import WindowLimiter

logger = logging.getLogger("agentic.testing_lab")

router = APIRouter(prefix="/api/testing-lab", tags=["testing-lab"])

# GETs are cheap reads. POSTs each start a training run, so they draw from their own small budget:
# a walk-forward over a few thousand bars is seconds to minutes of pinned CPU on a box that also
# serves this dashboard and shares a GPU with another project.
_READ_LIMITER = WindowLimiter()
_RUN_LIMITER = WindowLimiter()
_READ_BUDGET = (60, 60.0)
_RUN_BUDGET = (6, 60.0)

# Enumerated, not pattern-matched. See the module docstring. The write side needs no list — each
# POST is its own declared route below, which is a stronger allow-list than a tuple.
_READ_ROUTES = ("health", "parameters", "datasets", "experiments", "compare")

# A training run streams for as long as it trains. `read=None` disables the read timeout for the
# stream body only — connect and write stay bounded, so an unreachable Lab still fails fast instead
# of hanging a request forever.
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
_READ_TIMEOUT = httpx.Timeout(15.0)


def _base_url() -> str:
    """Where the Lab lives, or a clear 503.

    Unset is a supported state: the Lab is optional, and a deployment without it must still serve
    every other page. What it must NOT do is fail obscurely — an unset variable that turns into
    `http:///api/...` produces a connection error that reads like the Lab is broken rather than
    absent.
    """
    url = os.environ.get("LAB_BASE_URL", "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=503,
            detail=(
                "The Testing Lab is not deployed on this stack. Set LAB_BASE_URL and start the "
                "`lab` service (deploy/docker-compose.prod.yml)."
            ),
        )
    return url


def _gate(limiter: WindowLimiter, budget: tuple[int, float], what: str) -> None:
    wait = limiter.check_and_consume(*budget)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"too many {what} requests; retry in {wait}s",
            headers={"Retry-After": str(wait)},
        )


def _sse_error(message: str) -> bytes:
    """One SSE error event, framed exactly as lab/app.py and backend/app/sse.py frame theirs."""
    return f"data: {json.dumps({'type': 'error', 'message': message})}\n\n".encode()


def _attributed(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Stamp the authenticated operator onto the body, replacing anything the client sent.

    Same derivation as routers/settings.py::update_setting, deliberately identical: the email if
    the session carries one, else the operator's own repr. `None` when session enforcement is
    standing down — recorded as an absent operator rather than invented, since a run attributed to
    "unknown" and a run attributed to nobody are different facts.
    """
    operator = getattr(request.state, "operator", None)
    actor = None
    if operator is not None:
        actor = getattr(operator, "email", None) or str(operator)
    return {**body, "operator": actor}


async def _stream(path: str, body: dict[str, Any]) -> StreamingResponse:
    """Relay one of the Lab's SSE streams, chunk for chunk.

    The upstream client is opened inside the generator and closed when it finishes, so a client
    that disconnects mid-training tears down the connection to the Lab too — which the Lab sees as
    a cancellation and records on the experiment row, rather than leaving a job running for a
    stream nobody is reading.
    """

    async def relay() -> AsyncIterator[bytes]:
        try:
            async with (
                httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client,
                client.stream("POST", f"{_base_url()}/api/testing-lab/{path}", json=body) as up,
            ):
                if up.status_code >= 400:
                    detail = (await up.aread()).decode("utf-8", "replace")[:500]
                    logger.error("lab %s returned %s: %s", path, up.status_code, detail)
                    # json.dumps, NOT an f-string with !r. Python's repr quotes with apostrophes,
                    # which is not JSON — the first draft here emitted an event the frontend parser
                    # could not read, so a Lab error arrived as a silent stall instead of a message.
                    yield _sse_error(detail)
                    return
                async for chunk in up.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            # Reported as an SSE error event, not raised: the response has already begun, so an
            # exception here would truncate the stream and the client would see a silent stall.
            logger.error("lab stream %s failed: %s", path, exc)
            yield _sse_error("the Testing Lab is unreachable")

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{path:path}")
async def lab_read(path: str, request: Request) -> JSONResponse:
    """Proxy a Lab read. Only the enumerated prefixes are reachable."""
    if not any(path == r or path.startswith(f"{r}/") for r in _READ_ROUTES):
        raise HTTPException(status_code=404, detail="no such Testing Lab route")
    _gate(_READ_LIMITER, _READ_BUDGET, "Testing Lab")

    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            upstream = await client.get(
                f"{_base_url()}/api/testing-lab/{path}", params=dict(request.query_params)
            )
    except httpx.HTTPError as exc:
        logger.error("lab read %s failed: %s", path, exc)
        raise HTTPException(status_code=502, detail="the Testing Lab is unreachable") from exc

    return JSONResponse(
        status_code=upstream.status_code,
        content=upstream.json() if upstream.content else {},
        # Experiment results are not secret, but they are this operator's work and there is no
        # reason for an intermediary to keep a copy. Same posture as every other /api/ response.
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/experiments/run")
async def run_experiment(request: Request) -> StreamingResponse:
    _gate(_RUN_LIMITER, _RUN_BUDGET, "Testing Lab run")
    return await _stream("experiments/run", _attributed(await request.json(), request))


@router.post("/sweeps")
async def run_sweep(request: Request) -> StreamingResponse:
    _gate(_RUN_LIMITER, _RUN_BUDGET, "Testing Lab run")
    return await _stream("sweeps", _attributed(await request.json(), request))
