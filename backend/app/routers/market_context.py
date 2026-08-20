"""GET /api/market-context — what is coming up for the book, and what was said about it.

Contract: docs/contracts/market-context-endpoint.md. Read-only.

TWO SOURCES, TWO CONFIDENCE LEVELS, KEPT APART
    * CATALYSTS come from FMP's earnings calendar — dated, licensed, and authoritative. Available
      today on the current plan (verified: 2,051 rows for a two-week window).
    * HEADLINES come from Market Mover, a SEPARATE project that publishes a daily brief. It is
      editorial, it is third-party text, and it is not here yet.

    They are not merged. A dated earnings entry and a written market read are different kinds of
    claim, and blending them into one feed would make the provenance of any given line ambiguous.

WHEN THE BRIEF IS ABSENT, SAY SO
    No brief means `headlines: []` and a `brief_generated_at` of null — never an empty list dressed
    as a quiet day. The page can tell "nothing published" from "nothing happening" only if this
    endpoint distinguishes them, and only one of those is a reason to go looking.

THE BRIEF IS UNTRUSTED TEXT
    When it lands it is third-party content rendered to an operator. It is stored and served as
    DATA — never interpolated into a prompt, never treated as instructions. That constraint belongs
    with the ingest, and is written here because this is where the text becomes visible.

ADR-001, WHICH SHAPES THE MECHANISM
    rh-db has no network port. This route therefore serves an INGESTED brief from a path the backend
    already reads; it never opens a cross-project database link. The frontend is unaware either way.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.config import get_settings
from app.services.broker import get_snapshot
from app.services.freshness import is_stale
from app.services.slate import load_slate
from app.services.snapshot import SnapshotError

logger = logging.getLogger("agentic.api.market_context")

AccountId = Annotated[
    int | None,
    Query(ge=1, le=9, description="which configured account to read; omitted means the default"),
]

router = APIRouter(prefix="/api", tags=["market-context"])

# How far ahead to look for catalysts. Two weeks covers the rental window the slate cares about
# (3-5 days pre-print) with room to see one coming.
_CATALYST_HORIZON_DAYS = 14

# The slate's PLTR rule: enter 3-5 days pre-catalyst. The window is reported as open at <= 5 days
# and > 0 — a catalyst today is not an entry window, it is the event.
_RENTAL_WINDOW_DAYS = 5
_RENTAL_NAMES = ("PLTR",)

# A brief older than this is stale. One trading day, generously: a Friday brief read on Monday is
# still the most recent one published, and calling that stale would cry wolf every weekend.
_BRIEF_STALE_AFTER_HOURS = 36


def _brief_path():
    """Where the ingested Market Mover brief lands. A file the backend already reads (ADR-001)."""
    return get_settings().data_dir / "market_mover" / "latest.json"


def _load_brief() -> dict[str, Any] | None:
    path = _brief_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A malformed brief is reported as absent, not as an error page: catalysts are still worth
        # showing. But it is logged loudly, because silently serving no headlines when a brief EXISTS
        # is the difference between "nothing published" and "we could not read what was published".
        logger.error("market mover brief at %s is unreadable: %s", path.name, exc)
        return None


def _trading_days_until(target: date) -> int:
    """Whole days until a date, counting weekdays only.

    Deliberately not calendar days: "3 days until earnings" over a weekend is really one trading
    day, and a rental window computed on calendar days would open too early. Holidays are not
    modelled — market_calendar exists for that and is worth wiring when this matters more than it
    does today. Stated rather than silently approximated.
    """
    today = datetime.now(timezone.utc).date()
    if target <= today:
        return 0
    days = 0
    cursor = today
    while cursor < target:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _catalysts(symbols: set[str], slate: dict, held: set[str]) -> list[dict[str, Any]]:
    """Earnings dates for names the book cares about, from FMP.

    Filtered to slate + held names rather than the whole calendar: 2,051 entries for a fortnight is
    a market feed, not a watchlist, and a page showing every S&P print would bury the four that
    matter to this account.
    """
    if not symbols:
        return []
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=_CATALYST_HORIZON_DAYS)
    try:
        from src.fmp import get_shared_client

        rows = get_shared_client().get(
            "earnings-calendar",
            {"from": today.isoformat(), "to": horizon.isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 — a missing calendar must not fail the page
        logger.warning("earnings calendar unavailable: %s", exc)
        return []
    if not isinstance(rows, list):
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        if sym not in symbols:
            continue
        raw_date = row.get("date")
        try:
            when = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError):
            continue
        days_until = _trading_days_until(when)
        in_slate = sym in slate
        is_held = sym in held
        rental = (
            sym in _RENTAL_NAMES and in_slate and 0 < days_until <= _RENTAL_WINDOW_DAYS
        )
        note = None
        if rental and not is_held:
            note = "Rental window open; not currently held."
        elif in_slate and not is_held:
            note = "In the slate, not currently held."
        out.append({
            "symbol": sym,
            "label": "Earnings",
            "type": "earnings",
            "date": when.isoformat(),
            "days_until": days_until,
            "in_slate": in_slate,
            "held": is_held,
            "rental_window": rental,
            "note": note,
        })
    out.sort(key=lambda c: (c["date"], c["symbol"]))
    return out


@router.get("/market-context")
def market_context(account_id: AccountId = None) -> dict[str, Any]:
    settings = get_settings()
    docs = settings.docs_dir
    slate = load_slate(docs / "SLATE.md")

    held: set[str] = set()
    try:
        snapshot = get_snapshot(settings.snapshot_path, account_id)
        held = {p.symbol for p in snapshot.positions}
    except SnapshotError as exc:
        # Catalysts are still useful without the account: the page degrades to slate-only relevance
        # rather than going blank. `held` stays empty and every catalyst reports held=False, which
        # is honest — we could not confirm a holding, and the note says the slate is the only lens.
        logger.warning("account unavailable for market context; slate-only relevance: %s", exc)

    brief = _load_brief()
    headlines: list[dict[str, Any]] = []
    brief_generated_at = None
    macro_read = None
    if brief:
        brief_generated_at = brief.get("generated_at")
        macro_read = brief.get("macro_read")
        raw = brief.get("headlines")
        if isinstance(raw, list):
            for h in raw:
                if not isinstance(h, dict):
                    continue
                tickers = [
                    t.upper() for t in (h.get("tickers") or [])
                    if isinstance(t, str) and t.upper() in (set(slate) | held)
                ]
                headlines.append({
                    "id": h.get("id"),
                    "title": h.get("title"),
                    "source": h.get("source"),
                    "url": h.get("url"),
                    "published_at": h.get("published_at"),
                    "summary": h.get("summary"),
                    "tickers": tickers,
                    "sentiment": h.get("sentiment"),
                })

    brief_stale = is_stale(
        brief_generated_at, _BRIEF_STALE_AFTER_HOURS * 3600, field="market brief generated_at"
    )

    return {
        "meta": {
            "brief_generated_at": brief_generated_at,
            # True when no brief exists at all, which is a DIFFERENT state from a quiet news day and
            # must not render as one.
            "brief_stale": brief_stale,
            "brief_present": brief is not None,
            "source": "Market Mover",
            "catalyst_source": "FMP earnings calendar",
            "macro_read": macro_read,
        },
        "catalysts": _catalysts(set(slate) | held, slate, held),
        "headlines": headlines,
    }
