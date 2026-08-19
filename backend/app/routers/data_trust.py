"""GET /api/data-trust — can you believe the numbers on this page?

Feeds the always-visible strip the Shell renders above every page
(``frontend/src/components/data-trust.tsx``; contract in ``docs/contracts/data-trust-endpoint.md``).

WHY THIS ROUTE EXISTS
    For three weeks the Portfolio page showed holdings dated 27 July as though they were current.
    Prices were live, which made it harder to notice rather than easier — the numbers moved. Nothing
    on the screen said the positions underneath them were frozen, and the only reason anyone found
    out was that the owner asked.

    This strip is the thing that says so. It is deliberately small and deliberately pessimistic: it
    reports what it can prove and refuses to render a green state it has not earned.

CHEAP BY CONSTRUCTION
    It renders on Scan, Pipeline and Debate — pages that never show a portfolio. Proxying
    /api/account would make each of them pay for a full portfolio payload plus a broker round-trip
    for a status bar. So the snapshot is read once here (through the same short-TTL broker cache the
    account view uses) and priced against the same marks cache. No new upstream calls.

NOT A HEALTH CHECK
    /api/health answers "is the service up" for a container probe. This answers "is what you are
    looking at true", which is a different question with different consumers. They share two posture
    flags and nothing else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from app.config import get_settings
from app.services.broker import get_snapshot
from app.services.marks import MARKS_PROVIDER, get_marks_detailed, resolve_ttl_seconds
from app.services.snapshot import SnapshotError

logger = logging.getLogger("agentic.api.data_trust")

router = APIRouter(prefix="/api", tags=["data-trust"])

# Until dividends and corporate actions flow into the return series, every performance figure in
# this app is price-only. Surfaced as a standing caveat rather than a footnote nobody reads.
RETURNS_BASIS = "price_only"


def _parse_iso(value: str | None) -> datetime | None:
    """Parse the snapshot's ISO-8601 stamp, tolerating the trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("snapshot generated_at is not ISO-8601: %r", value)
        return None


def _budget_status() -> dict[str, Any]:
    """How much of the daily provider allowance this process has spent.

    Process-local and approximate by construction (src/fmp.py::CallBudget) — the key's responses
    carry no rate-limit headers. Approximate and visible beats exact and absent: the previous
    budget was sized for a backfill, the dashboard quietly outspent it every ten minutes, and
    nothing anywhere reported that until positions started disappearing.
    """
    try:
        from src.fmp import get_shared_client

        budget = get_shared_client().budget
        limit = int(budget.limit)
        spent = int(budget.spent)
    except Exception:  # noqa: BLE001 — a reporting failure must not fail the page
        return {"spent": None, "limit": None, "exhausted": None}
    return {
        "spent": spent,
        # 0 means no daily cap is configured, which is not the same as a cap of zero.
        "limit": limit or None,
        "exhausted": bool(limit and spent >= limit),
    }


@router.get("/data-trust")
def data_trust() -> dict[str, Any]:
    """Freshness, pricing coverage, and posture — the whole strip in one small payload."""
    settings = get_settings()
    # Imported here rather than at module scope so the strip reports auth posture through the same
    # function /api/health uses, without the two routers importing each other.
    from app.services.auth import auth_enforcement_configured

    posture: dict[str, Any] = {
        "returns_basis": RETURNS_BASIS,
        "debate_live": settings.anthropic_api_key is not None,
        "auth_enforced": auth_enforcement_configured(),
    }

    try:
        snapshot = get_snapshot(settings.snapshot_path)
    except SnapshotError as exc:
        # 200 with an honest empty state, NOT an error. "We have no fresh data" is a fact the strip
        # must be able to render; it is a different thing from the endpoint being unreachable, and
        # collapsing the two would leave the operator unable to tell "broker down" from "site down".
        logger.warning("data-trust: no account data available: %s", exc)
        return {
            "snapshot_generated_at": None,
            "snapshot_stale": True,
            "source": None,
            "price_source": MARKS_PROVIDER,
            "prices_degraded": True,
            "positions_total": 0,
            "positions_priced": 0,
            **posture,
        }

    generated = _parse_iso(snapshot.generated_at)
    if generated is None:
        # An unparseable stamp cannot be shown to be fresh, so it is stale. Freshness must be
        # PROVEN, never assumed — assuming it is how the July snapshot went unremarked.
        stale = True
    else:
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        stale = age > settings.snapshot_max_age_seconds

    symbols = snapshot.symbols
    ttl = resolve_ttl_seconds(settings.marks_ttl_seconds)
    detailed = get_marks_detailed(symbols, ttl) if symbols else {}
    priced = sum(1 for s in symbols if detailed.get(s) and detailed[s].price is not None)
    # Named, not just counted. "9/10 priced" leaves the operator hunting for the missing one, and a
    # stale-but-served price is a different state from an absent one — both were invisible during
    # the 2026-08-19 outage, when every position silently went unpriced at once.
    unpriced = sorted(s for s in symbols if not (detailed.get(s) and detailed[s].price is not None))
    stale_served = sorted(s for s in symbols if detailed.get(s) and detailed[s].stale)

    return {
        "snapshot_generated_at": snapshot.generated_at,
        "snapshot_stale": stale,
        # Which account this is. The dashboard shows one at a time and the source changed under
        # everyone once already; leaving it to be inferred from context is how that goes unnoticed.
        "source": snapshot.source,
        "price_source": MARKS_PROVIDER,
        "prices_degraded": priced < len(symbols),
        "positions_total": len(symbols),
        "positions_priced": priced,
        "unpriced_symbols": unpriced,
        # Priced, but from a cache the provider would not refresh. Real numbers, older than the
        # refresh interval, and the page must be able to say so rather than showing them as live.
        "stale_priced_symbols": stale_served,
        "price_refresh_seconds": ttl,
        # The quota that took the dashboard down. Surfaced so exhaustion is visible while there is
        # still headroom, instead of arriving as a blank table.
        "provider_budget": _budget_status(),
        **posture,
    }
