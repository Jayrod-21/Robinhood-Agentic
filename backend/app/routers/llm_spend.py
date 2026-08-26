"""GET /api/llm-spend — who has paid for the model calls, and how far off even it is.

The question this answers is not "what did we spend" but "what does each of us owe". Those differ:
a total is one number and a settlement needs the split, the spread, and an honest note about how
much of it is estimated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.llm import ledger

router = APIRouter(prefix="/api", tags=["llm-spend"])


@router.get("/llm-spend")
def llm_spend() -> dict[str, Any]:
    """Per-owner spend, the spread between owners, and what is not priced.

    Costs are estimated from a documented price list rather than a billing API, so the response says
    so rather than presenting an estimate as an invoice. `unpriced_rows` per owner is the part of
    their usage that has tokens but no dollars — surfaced so a total is never read as complete when
    it is not.
    """
    return ledger.totals()
