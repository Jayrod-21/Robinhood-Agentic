"""Scan endpoints: stream the real Sprinkle Sauce screen over a ticker universe.

This reuses 3b's tested ``src`` screen verbatim — yfinance fundamentals → tiered gates → composite
score — and streams one result per ticker so the dashboard fills in live, ending with a ranked
survivor list. No LLM, no cost.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.ratelimit import scan_limiter
from app.sse import sse_response
from app.validation import normalize_ticker

logger = logging.getLogger("agentic.routers.scan")

router = APIRouter(prefix="/api/scan", tags=["scan"])

# Minimum seconds between honored scan kickoffs (F4). Each scan fans out one blocking yfinance fetch
# per ticker — a whole universe with an empty body — so back-to-back requests multiply outbound Yahoo
# traffic and risk an IP ban. Module constant (Settings is owned elsewhere); 15s matches the default
# cadence of the sibling debate/pipeline cooldown.
SCAN_MIN_INTERVAL_SECONDS = 15

# Hard ceiling enforced by pydantic at parse time (422) — a coarse backstop against a pathological
# multi-thousand-element body. The finer, operator-tunable cap (scan_max_tickers) is applied in the
# handler with a clear 400 so a normal-but-too-large request gets an actionable message, not a 422.
_MAX_TICKERS_ABSOLUTE = 500


class ScanRequest(BaseModel):
    tickers: list[str] | None = Field(default=None, max_length=_MAX_TICKERS_ABSOLUTE)
    min_cap: float | None = Field(default=None, gt=0)


def _screen_one(ticker: str, min_cap: float) -> dict:
    """Blocking fetch + screen for one ticker → a JSON-friendly summary."""
    from src.data import fetch_fundamentals
    from src.screen import screen_ticker

    fundamentals = fetch_fundamentals(ticker)
    if fundamentals is None:
        return {"ticker": ticker, "ok": False, "passed": False, "reason": "no data (yfinance miss)"}
    res = screen_ticker(ticker, fundamentals, min_market_cap=min_cap)
    ss = res.tiers.get("sprinkle_sauce")
    return {
        "ticker": ticker,
        "ok": True,
        "passed": res.passed,
        "failed_tier": res.failed_tier,
        "composite": res.composite,
        "reason": (res.reasons[0] if res.reasons else None),
        "peg": ss.metrics.get("peg") if ss else None,
        "fcf_yield": ss.metrics.get("fcf_yield") if ss else None,
        "name": fundamentals.get("name"),
        "sector": fundamentals.get("sector"),
    }


async def _run_scan(tickers: list[str], min_cap: float):
    yield {"type": "scan_start", "count": len(tickers), "min_cap": min_cap}
    results: list[dict] = []
    for i, ticker in enumerate(tickers):
        row = await asyncio.to_thread(_screen_one, ticker, min_cap)
        results.append(row)
        yield {"type": "scan_result", "index": i, "total": len(tickers), "result": row}
    survivors = sorted(
        (r for r in results if r.get("passed")),
        key=lambda r: r.get("composite") or 0.0,
        reverse=True,
    )
    yield {"type": "scan_complete", "survivors": survivors, "scanned": len(results)}


@router.post("/run-stream")
def run_stream(req: ScanRequest):
    from src.daily_scan import DEFAULT_MIN_CAP
    from src.universe import flat_universe

    settings = get_settings()
    min_cap = req.min_cap or DEFAULT_MIN_CAP
    if req.tickers:
        # B3: cap the user-supplied list so one request can't fan out unbounded blocking yfinance
        # fetches. Reject (not silently truncate) so the caller knows the request was too large.
        if len(req.tickers) > settings.scan_max_tickers:
            raise HTTPException(
                status_code=400,
                detail=f"Too many tickers ({len(req.tickers)}); max {settings.scan_max_tickers} per scan.",
            )
        # S4: validate up front and reject all-invalid input rather than silently producing a no-op
        # scan that's indistinguishable from "scanned everything, nothing passed".
        accepted: list[str] = []
        rejected: list[str] = []
        for raw in req.tickers:
            sym = normalize_ticker(raw)
            (accepted if sym else rejected).append(sym or raw)
        if not accepted:
            raise HTTPException(
                status_code=400,
                detail=f"No valid tickers in request; rejected: {rejected}",
            )
        tickers = accepted
    else:
        tickers = flat_universe()

    # F4: cooldown gate, checked AFTER the cheap request validation above so a malformed request
    # gets its actionable 400 without consuming the budget — only a request that would actually
    # fan out yfinance fetches draws from it. Same CooldownLimiter pattern as debate/pipeline, but
    # a separate instance (see app.ratelimit): scans are free and must not block paid debates.
    wait = scan_limiter.check_and_consume(SCAN_MIN_INTERVAL_SECONDS)
    if wait:
        # Never block silently: say so in the server log (with the why) and in the 429 detail.
        logger.warning(
            "scan rate limit hit: %s ticker(s) requested, ~%ss of cooldown left "
            "(min interval %ss between scans to avoid hammering Yahoo)",
            len(tickers),
            wait,
            SCAN_MIN_INTERVAL_SECONDS,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Scan rate limit — wait ~{wait}s (each scan fans out one Yahoo fetch per ticker).",
        )
    return sse_response(_run_scan(tickers, min_cap))


@router.get("/universe")
def universe() -> dict:
    from src.daily_scan import DEFAULT_MIN_CAP
    from src.universe import WATCHLIST, flat_universe

    return {"groups": WATCHLIST, "flat": flat_universe(), "default_min_cap": DEFAULT_MIN_CAP}
