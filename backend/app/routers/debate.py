"""Debate endpoints: run a live jury debate (SSE) and read past debate records.

Each debate spends real Anthropic tokens, so the run endpoint validates the ticker and rate-limits
how often a debate can be kicked off (a cheap abuse guard on a paid path).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.debate.engine import run_debate
from app.debate.records import get_record, list_records
from app.ratelimit import debate_limiter
from app.sse import sse_response
from app.validation import validate_ticker

router = APIRouter(prefix="/api/debate", tags=["debate"])


class DebateRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=6)
    question: str | None = Field(default=None, max_length=300)


@router.post("/run-stream")
def run_stream(req: DebateRequest):
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="Live debates need ANTHROPIC_API_KEY in backend/.env.",
        )

    # Shared cooldown with the pipeline endpoint — both spend tokens, so they draw from one budget.
    wait = debate_limiter.check_and_consume(settings.debate_min_interval_seconds)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Debate rate limit — wait ~{wait}s (each debate costs tokens).",
        )

    ticker = validate_ticker(req.ticker)
    return sse_response(run_debate(ticker, req.question))


@router.get("/records")
def records() -> list[dict]:
    return list_records()


@router.get("/{record_id}")
def record(record_id: str) -> dict:
    rec = get_record(record_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"No debate record {record_id!r}")
    return rec
