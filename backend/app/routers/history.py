"""History + DB-health endpoints (issues #6 / #4).

Degradation contract, stated once and enforced everywhere below:

  * ``GET /api/db/health`` ALWAYS answers 200 with a structured status — it is how the frontend
    (and the operator) learns the database is absent, so it must work exactly when the DB doesn't.
  * Every ``/api/history/*`` route answers 503 with a clear, non-secret reason when the database
    is unavailable. The rest of the dashboard (account / scan / pipeline / debate) never touches
    this layer and keeps serving — that property is pinned by tests/test_history_router.py.

Writes here go through ``services/outcomes.py``, which is shaped around the append-only grants
from migrations 004/011: exits update only their four granted columns, knowledge-base rows are
insert-only, corrections supersede. Domain refusals (double exit, unknown symbol) come back as
409 with the exact reason — loud, specific, never a silent no-op (SENIOR_ENGINEER_BAR §7.2).

State-changing routes are covered by the app-wide CSRF guard registered in ``create_app``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import DbUnavailable, db_health
from app.services import outcomes

logger = logging.getLogger("agentic.history")

router = APIRouter(prefix="/api", tags=["history"])

# One symbol grammar for every route, matching 001's ck_securities_symbol so a bad symbol fails
# fast as a 422 here instead of a constraint error deep in the write path.
_SYMBOL_PATTERN = r"^[A-Za-z][A-Za-z0-9]{0,9}(\.[A-Za-z0-9]{1,4}){0,2}$"


def _unavailable(exc: DbUnavailable) -> HTTPException:
    # 503, not 500: the service is healthy, a dependency is absent — and the message says which.
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/db/health")
def database_health() -> dict[str, Any]:
    """Database status: configured / reachable / role / schema version. Never 500s."""
    return db_health()


@router.get("/history/entries")
def list_entries(
    entry_type: Annotated[
        Literal["thesis", "outcome", "lesson", "postmortem", "note"] | None, Query()
    ] = None,
    symbol: Annotated[str | None, Query(pattern=_SYMBOL_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Knowledge-base entries, newest first — the track record future debates read."""
    try:
        entries = outcomes.recent_entries(entry_type=entry_type, symbol=symbol, limit=limit)
    except DbUnavailable as exc:
        raise _unavailable(exc) from exc
    return {"entries": entries, "count": len(entries)}


class ThesisRequest(BaseModel):
    symbol: str = Field(pattern=_SYMBOL_PATTERN, examples=["TSM"])
    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=20_000)
    portfolio_id: int | None = Field(default=None, ge=1)
    debate_id: int | None = Field(default=None, ge=1)
    agent_id: int | None = Field(default=None, ge=1)
    as_of: datetime | None = None


@router.post("/history/thesis", status_code=201)
def create_thesis(req: ThesisRequest) -> dict[str, Any]:
    """Record an entry thesis (knowledge-base 'thesis' row)."""
    try:
        entry_id = outcomes.record_entry_thesis(
            symbol=req.symbol,
            title=req.title,
            thesis=req.thesis,
            portfolio_id=req.portfolio_id,
            debate_id=req.debate_id,
            agent_id=req.agent_id,
            as_of=req.as_of,
        )
    except DbUnavailable as exc:
        raise _unavailable(exc) from exc
    except outcomes.OutcomeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": entry_id}


class ExitRequest(BaseModel):
    portfolio_id: int = Field(ge=1)
    symbol: str = Field(pattern=_SYMBOL_PATTERN, examples=["TSM"])
    entry_date: date
    exit_date: date
    # Decimal end to end (Bar §7.2 P0): pydantic parses the JSON number/string to Decimal without
    # a float in between when it arrives as a string; clients should send prices as strings.
    exit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    exit_reason: str = Field(min_length=1, max_length=2_000)
    lesson: str | None = Field(default=None, max_length=20_000)
    agent_id: int | None = Field(default=None, ge=1)


@router.post("/history/exits", status_code=201)
def create_exit(req: ExitRequest) -> dict[str, Any]:
    """Close a lot: exit columns + realized P&L + the 'outcome' knowledge-base row, atomically."""
    try:
        record = outcomes.record_exit(
            portfolio_id=req.portfolio_id,
            symbol=req.symbol,
            entry_date=req.entry_date,
            exit_date=req.exit_date,
            exit_price=req.exit_price,
            exit_reason=req.exit_reason,
            lesson=req.lesson,
            agent_id=req.agent_id,
        )
    except DbUnavailable as exc:
        raise _unavailable(exc) from exc
    except outcomes.OutcomeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "portfolio_id": record.portfolio_id,
        "symbol": record.symbol,
        "entry_date": record.entry_date.isoformat(),
        "exit_date": record.exit_date.isoformat(),
        "shares": str(record.shares),
        "entry_price": str(record.entry_price),
        "exit_price": str(record.exit_price),
        "exit_reason": record.exit_reason,
        "realized_pnl": str(record.realized_pnl),
        "outcome_entry_id": record.outcome_entry_id,
    }


class LessonRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    lesson: str = Field(min_length=1, max_length=20_000)
    body: str | None = Field(default=None, max_length=50_000)
    symbol: str | None = Field(default=None, pattern=_SYMBOL_PATTERN)
    portfolio_id: int | None = Field(default=None, ge=1)
    debate_id: int | None = Field(default=None, ge=1)
    agent_id: int | None = Field(default=None, ge=1)
    supersedes_id: int | None = Field(default=None, ge=1)
    as_of: datetime | None = None


@router.post("/history/lessons", status_code=201)
def create_lesson(req: LessonRequest) -> dict[str, Any]:
    """Record a lesson (append-only; corrections supersede, never edit)."""
    try:
        entry_id = outcomes.record_lesson(
            title=req.title,
            lesson=req.lesson,
            body=req.body,
            symbol=req.symbol,
            portfolio_id=req.portfolio_id,
            debate_id=req.debate_id,
            agent_id=req.agent_id,
            supersedes_id=req.supersedes_id,
            as_of=req.as_of,
        )
    except DbUnavailable as exc:
        raise _unavailable(exc) from exc
    except outcomes.OutcomeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": entry_id}
