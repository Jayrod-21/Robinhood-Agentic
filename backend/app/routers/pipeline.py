"""Pipeline endpoint — the 3a-style node stepper for a single ticker.

Wraps the debate engine and adds a real screening node up front, translating the engine's event
stream into discrete pipeline nodes (screen → bull → bear → jury → decision) so the frontend can
render the vertical stepper with live per-node status. Reuses the engine's one context fetch — the
screen node scores the same fundamentals the debate already pulled.

Each completed run is also persisted as a `PipelineRunRecord` (issue #28) so `GET /history` can
show every ticker ever run with its price-at-run against the current mark. The store is
DELIBERATELY file-backed for now — see the pipeline section of ``app/debate/records.py`` for the
full rationale (the DB evaluation tables exist but nothing writes them yet; a separate workstream
is wiring that layer).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.debate.engine import run_debate
from app.debate.records import PipelineRunRecord, list_pipeline_runs, persist_pipeline_run
from app.ratelimit import debate_limiter
from app.services import settings_store
from app.services.marks import get_marks, resolve_ttl_seconds
from app.sse import sse_response
from app.validation import validate_ticker

logger = logging.getLogger("agentic.routers.pipeline")

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

NODES = ["screen", "bull", "bear", "jury", "decision"]

# Cap on history rows returned (and marked): bounds the FMP fan-out for the current-price
# overlay no matter how large the JSONL file grows.
HISTORY_LIMIT = 200


class PipelineRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=6)


def _screen(ticker: str, fundamentals: dict | None) -> dict:
    from src.daily_scan import DEFAULT_MIN_CAP
    from src.screen import screen_ticker

    if fundamentals is None:
        return {"passed": False, "reason": "no fundamentals (FMP returned nothing for this symbol)"}
    res = screen_ticker(ticker, fundamentals, min_market_cap=DEFAULT_MIN_CAP)
    return {
        "passed": res.passed,
        "failed_tier": res.failed_tier,
        "composite": res.composite,
        "reason": (res.reasons[0] if res.reasons else None),
    }


def _build_run_record(ticker: str, screen: dict | None, debate_record: dict) -> PipelineRunRecord:
    """Shape one completed run into the persisted history row.

    ``debate_record`` is the engine's ``record.model_dump()`` (python mode), so enum members may
    arrive as enum instances — normalize to their plain string values before persisting.
    """
    decision = debate_record.get("final_decision")
    if decision is not None:
        decision = getattr(decision, "value", decision)
    jury = debate_record.get("jury") or {}
    screen = screen or {}
    now = datetime.now(timezone.utc)
    return PipelineRunRecord(
        id=f"plr-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
        ticker=ticker,
        created_at=debate_record.get("created_at") or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        debate_id=debate_record.get("id"),
        price_at_run=debate_record.get("price"),
        screen_passed=screen.get("passed"),
        screen_composite=screen.get("composite"),
        screen_reason=screen.get("reason") or screen.get("failed_tier"),
        decision=decision,
        escalated=bool(jury.get("escalated_to_human")),
    )


async def _run_pipeline(ticker: str):
    yield {"type": "pipeline_start", "ticker": ticker, "nodes": NODES}
    yield {"type": "node_start", "node": "screen"}
    jury_started = False
    screen: dict | None = None

    async for ev in run_debate(ticker):
        kind = ev.get("type")
        if kind == "context":
            screen = await asyncio.to_thread(_screen, ticker, ev.get("fundamentals"))
            yield {"type": "node_complete", "node": "screen",
                   "data": {**screen, "price": ev.get("price")}}
            yield {"type": "node_start", "node": "bull"}
            yield {"type": "node_start", "node": "bear"}
        elif kind == "bull_complete":
            yield {"type": "node_complete", "node": "bull", "data": {"bull_case": ev["bull_case"]}}
        elif kind == "bear_complete":
            yield {"type": "node_complete", "node": "bear", "data": {"bear_case": ev["bear_case"]}}
        elif kind == "juror_complete":
            if not jury_started:
                jury_started = True
                yield {"type": "node_start", "node": "jury"}
            yield {"type": "node_progress", "node": "jury",
                   "vote": ev["vote"], "completed": ev["completed"], "total": ev["total"]}
        elif kind == "aggregate":
            yield {"type": "node_complete", "node": "jury", "data": ev["jury"]}
            yield {"type": "node_start", "node": "decision"}
        elif kind == "decision":
            yield {"type": "node_complete", "node": "decision", "data": ev}
        elif kind == "debate_complete":
            # Persist the run for GET /history. Only COMPLETED runs are recorded — an errored run
            # has no decision (and often no price) to compare, so it would be a blank history row.
            # Persistence failure must never break the stream the user is watching.
            try:
                record = _build_run_record(ticker, screen, ev["record"])
                await asyncio.to_thread(persist_pipeline_run, record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to persist pipeline run for %s: %s", ticker, exc)
            yield {"type": "pipeline_complete", "record": ev["record"]}
        elif kind == "error":
            yield {"type": "pipeline_error", "message": ev["message"]}
            return


def _build_history() -> list[dict]:
    """History rows with the entry-vs-current comparison computed server-side (issue #28).

    Current marks come from ``app.services.marks.get_marks`` — the shared TTL-cached FMP
    layer the account page already uses — never a second pricing path. An unpriceable symbol
    degrades to ``priced: False`` with null deltas rather than failing the request.
    """
    settings = get_settings()
    runs = list_pipeline_runs(limit=HISTORY_LIMIT)
    symbols = sorted({r["ticker"] for r in runs if r.get("ticker")})
    marks = get_marks(symbols, resolve_ttl_seconds(settings.marks_ttl_seconds)) if symbols else {}

    out: list[dict] = []
    for run in runs:
        entry = run.get("price_at_run")
        current = marks.get(run["ticker"])
        delta = delta_pct = None
        # Guard entry <= 0 (a corrupt/zero record) — a nonsense percent is worse than a dash.
        if current is not None and entry is not None and entry > 0:
            delta = round(current - entry, 2)
            delta_pct = round((current - entry) / entry * 100.0, 2)
        out.append(
            {
                **run,
                "current_price": round(current, 4) if current is not None else None,
                "delta": delta,
                "delta_pct": delta_pct,
                "priced": current is not None,
            }
        )
    return out


@router.get("/history")
async def history() -> list[dict]:
    """Every persisted pipeline run, newest first, with price-at-run vs current mark."""
    # FMP I/O in get_marks is blocking; keep it off the event loop (same as /api/account).
    return await asyncio.to_thread(_build_history)


@router.post("/run-stream")
def run_stream(req: PipelineRequest):
    settings = get_settings()
    if not settings.anthropic_api_key:
        # Same leak class as issue #13, already fixed in routers/debate.py: naming the variable and
        # the file to an unauthenticated caller maps the deployment for them. Operator detail goes
        # to the log, the client gets the fact that it is unavailable.
        logger.error(
            "pipeline jury requested but no API key is configured; "
            "set ANTHROPIC_API_KEY in backend/.env"
        )
        raise HTTPException(
            status_code=503,
            detail="Pipeline jury is not configured on the server.",
        )

    # The pipeline wraps run_debate, so it spends Anthropic tokens just like the debate endpoint.
    # Share the SAME limiter as debate.py so the combined spend honors one budget, not two.
    # The tuned cooldown, not the env default. The Parameters page showed 60 while this read 15,
    # so an operator raising it to stop a double-click spending tokens changed nothing.
    cooldown = int(settings_store.get_or("debate_min_interval_s", settings.debate_min_interval_seconds))
    wait = debate_limiter.check_and_consume(cooldown)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Pipeline rate limit — wait ~{wait}s (each run costs tokens).",
        )

    ticker = validate_ticker(req.ticker)
    return sse_response(_run_pipeline(ticker))
