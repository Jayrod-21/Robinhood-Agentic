"""Pipeline endpoint — the 3a-style node stepper for a single ticker.

Wraps the debate engine and adds a real screening node up front, translating the engine's event
stream into discrete pipeline nodes (screen → bull → bear → jury → decision) so the frontend can
render the vertical stepper with live per-node status. Reuses the engine's one context fetch — the
screen node scores the same fundamentals the debate already pulled.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.debate.engine import run_debate
from app.ratelimit import debate_limiter
from app.sse import sse_response
from app.validation import validate_ticker

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

NODES = ["screen", "bull", "bear", "jury", "decision"]


class PipelineRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=6)


def _screen(ticker: str, fundamentals: dict | None) -> dict:
    from src.daily_scan import DEFAULT_MIN_CAP
    from src.screen import screen_ticker

    if fundamentals is None:
        return {"passed": False, "reason": "no fundamentals (yfinance miss)"}
    res = screen_ticker(ticker, fundamentals, min_market_cap=DEFAULT_MIN_CAP)
    return {
        "passed": res.passed,
        "failed_tier": res.failed_tier,
        "composite": res.composite,
        "reason": (res.reasons[0] if res.reasons else None),
    }


async def _run_pipeline(ticker: str):
    yield {"type": "pipeline_start", "ticker": ticker, "nodes": NODES}
    yield {"type": "node_start", "node": "screen"}
    jury_started = False

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
            yield {"type": "pipeline_complete", "record": ev["record"]}
        elif kind == "error":
            yield {"type": "pipeline_error", "message": ev["message"]}
            return


@router.post("/run-stream")
def run_stream(req: PipelineRequest):
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="Pipeline jury needs ANTHROPIC_API_KEY in backend/.env.")

    # The pipeline wraps run_debate, so it spends Anthropic tokens just like the debate endpoint.
    # Share the SAME limiter as debate.py so the combined spend honors one budget, not two.
    wait = debate_limiter.check_and_consume(settings.debate_min_interval_seconds)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Pipeline rate limit — wait ~{wait}s (each run costs tokens).",
        )

    ticker = validate_ticker(req.ticker)
    return sse_response(_run_pipeline(ticker))
