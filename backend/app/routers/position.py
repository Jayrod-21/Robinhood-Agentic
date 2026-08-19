"""GET /api/position/{symbol} — one name, everything on record about it.

Contract: docs/contracts/position-endpoint.md. Read-only.

WHAT THIS PAGE IS FOR
    Joining the four places a name exists: what the broker actually holds, what the slate says it
    SHOULD be, what case was written for it, and what the last debate concluded. Those live in four
    different stores and have never been shown together, which is how a position drifts from its
    thesis without anyone noticing.

A DOCUMENTED NAME THAT IS NOT HELD IS NOT A 404
    The contract is explicit and it is the right call: a name the slate documents but the broker
    does not hold is exactly what an operator needs to see. It renders with target, thesis and last
    debate, and ``live``/``stop`` come back null. 404 is reserved for a symbol nothing knows about.

THE HONESTY FIELDS ARE NOT DECORATION
    ``snapshot_stale``, ``price_source``, ``held``, ``in_slate`` and a null ``thesis.summary`` are
    the difference between a drill-down that informs and one that quietly implies everything is
    fine. A held name with no thesis is a FINDING — the page is built to shout about it — so this
    endpoint must report the absence rather than omit the field.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.debate.records import get_record, list_records
from app.services.broker import get_snapshot
from app.services.marks import MARKS_PROVIDER, get_marks, resolve_ttl_seconds
from app.services.slate import load_sizing_rules, load_slate, load_theses
from app.services.snapshot import SnapshotError
from app.validation import normalize_ticker

logger = logging.getLogger("agentic.api.position")

router = APIRouter(prefix="/api", tags=["position"])

# Trading days of daily closes for the sparkline. ~90 is what the contract asks for; more is a
# bigger FMP payload for a chart nobody zooms into.
_HISTORY_DAYS = 130





def _price_history(symbol: str) -> list[dict[str, Any]]:
    """Daily closes from FMP. An empty list on failure — never a fabricated series.

    The contract says so and it matters: a chart invented to avoid an empty state is a lie told in
    a shape people trust more than text.
    """
    try:
        from src.fmp import get_shared_client, to_fmp_symbol

        rows = get_shared_client().get(
            "historical-price-eod/full",
            # Broker spelling -> vendor spelling. BRK.B vs BRK-B returns an EMPTY series rather
            # than an error, so without this the chart is silently blank for class shares.
            {"symbol": to_fmp_symbol(symbol), "from": _history_start()},
        )
    except Exception as exc:  # noqa: BLE001 — a missing chart must not fail the page
        logger.warning("price history unavailable for %s: %s", symbol, exc)
        return []
    if not isinstance(rows, list):
        return []
    out = [
        {"date": r.get("date"), "close": r.get("close")}
        for r in rows
        if r.get("date") and r.get("close") is not None
    ]
    out.sort(key=lambda r: r["date"])
    return out


def _history_start() -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=_HISTORY_DAYS * 1.5)).strftime("%Y-%m-%d")


def _last_debate(symbol: str) -> dict[str, Any] | None:
    """The most recent debate for this ticker, compressed.

    Debate records are files; each carries a ``ticker`` field, so this is a scan of the index rather
    than a search through prose. Returns None when nothing has been debated — an absence the page
    renders as "no debate on record", not as an error.
    """
    try:
        records = list_records()
    except Exception as exc:  # noqa: BLE001
        logger.warning("debate index unavailable: %s", exc)
        return None
    matches = [r for r in records if (r.get("ticker") or "").upper() == symbol]
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    record_id = matches[0].get("id")
    try:
        full = get_record(record_id) if record_id else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("debate record %s unreadable: %s", record_id, exc)
        return None
    if not full:
        return None

    jury = full.get("jury") or {}
    votes = jury.get("votes") if isinstance(jury, dict) else None
    counts: dict[str, int] = {}
    if isinstance(votes, list):
        for v in votes:
            verdict = str((v or {}).get("verdict") or "").upper()
            if verdict:
                counts[verdict] = counts.get(verdict, 0) + 1
    bull_bear = full.get("bull_bear") or {}
    decision = full.get("final_decision") or {}

    return {
        "id": full.get("id"),
        "created_at": full.get("created_at"),
        "question": full.get("question"),
        "decision": (decision.get("verdict") if isinstance(decision, dict) else None) or None,
        "escalated": bool(isinstance(decision, dict) and decision.get("escalated")),
        "bull_case": (bull_bear.get("bull") if isinstance(bull_bear, dict) else None),
        "bear_case": (bull_bear.get("bear") if isinstance(bull_bear, dict) else None),
        "jury_counts": counts or None,
        "jury_total": sum(counts.values()) if counts else None,
    }


def _thesis_status(
    *, held: bool, has_thesis: bool, breached: bool, unrealized_pct: float | None, drift: float | None
) -> str:
    """THE RULE, stated as the contract asks.

    * ``broken``  — the stop is breached, OR the name is held with no case on record. A held
      position nobody has written a reason for is broken by definition, whatever the P&L says;
      that is the judgement the charter's sell-discipline rule encodes.
    * ``watch``   — within 5 points of the stop, or drifted 5+ points from target.
    * ``intact``  — everything else.
    """
    if breached:
        return "broken"
    if held and not has_thesis:
        return "broken"
    if unrealized_pct is not None and unrealized_pct <= -15.0:
        return "watch"
    if drift is not None and abs(drift) >= 5.0:
        return "watch"
    return "intact"


@router.get("/position/{symbol}")
def position(symbol: str) -> dict[str, Any]:
    ticker = normalize_ticker(symbol)
    if ticker is None:
        raise HTTPException(status_code=400, detail="Not a valid ticker symbol.")

    settings = get_settings()
    docs = settings.docs_dir
    slate = load_slate(docs / "SLATE.md")
    theses = load_theses(docs / "THESES.md")
    rules = load_sizing_rules(docs / "SLATE.md")

    try:
        snapshot = get_snapshot(settings.snapshot_path)
    except SnapshotError as exc:
        # 503, not an empty page: "we cannot read the account" and "you do not hold this" are
        # different answers and must not look the same.
        raise HTTPException(status_code=503, detail=str(exc)) from None

    holding = next((p for p in snapshot.positions if p.symbol == ticker), None)
    entry = slate.get(ticker)
    thesis = theses.get(ticker)
    debate = _last_debate(ticker)

    if holding is None and entry is None and thesis is None and debate is None:
        raise HTTPException(status_code=404, detail=f"Nothing on record for {ticker}.")

    live: dict[str, Any] | None = None
    stop: dict[str, Any] | None = None
    weight_account_pct: float | None = None
    unrealized_pct: float | None = None

    if holding is not None:
        price = get_marks([ticker], resolve_ttl_seconds(settings.marks_ttl_seconds)).get(ticker)
        cost_basis = holding.quantity * holding.average_buy_price
        market_value = holding.quantity * price if price is not None else None
        unrealized = (market_value - cost_basis) if market_value is not None else None
        unrealized_pct = (unrealized / cost_basis * 100.0) if unrealized is not None and cost_basis else None
        total = snapshot.account.total_value
        weight_account_pct = (market_value / total * 100.0) if market_value is not None and total else None
        live = {
            "quantity": holding.quantity,
            "average_buy_price": holding.average_buy_price,
            "current_price": price,
            "cost_basis": round(cost_basis, 2),
            "market_value": round(market_value, 2) if market_value is not None else None,
            "unrealized_pl": round(unrealized, 2) if unrealized is not None else None,
            "unrealized_pl_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
            "weight_account_pct": round(weight_account_pct, 2) if weight_account_pct is not None else None,
            "weight_pct": None,
            # An unpriced holding is reported, not hidden: every number above it is then unknown.
            "priced": price is not None,
        }
        breached = unrealized_pct is not None and unrealized_pct <= rules.hard_stop_pct
        trim_line = entry.target_weight_pct * rules.trim_multiple if entry else None
        stop = {
            "hard_stop_pct": rules.hard_stop_pct,
            "distance_to_stop_pct": (
                round(unrealized_pct - rules.hard_stop_pct, 2) if unrealized_pct is not None else None
            ),
            "breached": bool(breached),
            "trim_line_weight_pct": round(trim_line, 2) if trim_line is not None else None,
            "above_trim_line": (
                bool(weight_account_pct > trim_line)
                if weight_account_pct is not None and trim_line is not None
                else None
            ),
        }

    drift = None
    if entry is not None and weight_account_pct is not None:
        drift = round(weight_account_pct - entry.target_weight_pct, 2)

    history = _price_history(ticker)
    generated = snapshot.generated_at
    stale = True
    try:
        gen_dt = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        stale = (datetime.now(timezone.utc) - gen_dt).total_seconds() > settings.snapshot_max_age_seconds
    except (ValueError, AttributeError):
        pass  # unparseable stamp stays stale — freshness is proven, never assumed

    return {
        "meta": {
            "symbol": ticker,
            "name": None,
            "sector": None,
            "snapshot_generated_at": generated,
            "snapshot_stale": stale,
            "source": snapshot.source,
            "price_source": MARKS_PROVIDER,
            "price_history_from": history[0]["date"] if history else None,
            "held": holding is not None,
        },
        "live": live,
        "slate": {
            "in_slate": entry is not None,
            "in_universe": entry is not None or thesis is not None,
            "target_weight_pct": entry.target_weight_pct if entry else None,
            "role": entry.role if entry else None,
            "size_rationale": entry.size_rationale if entry else None,
            "drift_pct": drift,
        },
        "stop": stop,
        "thesis": {
            "status": _thesis_status(
                held=holding is not None,
                has_thesis=thesis is not None,
                breached=bool(stop and stop["breached"]),
                unrealized_pct=unrealized_pct,
                drift=drift,
            ),
            # None, deliberately, when nothing is on record. The page turns that into a loud
            # "needs a case or an exit" — omitting the field would let it render as merely quiet.
            "summary": (thesis.core or thesis.headline) if thesis else None,
            "conviction": thesis.conviction if thesis else None,
            "updated_at": None,
        },
        "price_history": history,
        "debate": debate,
    }
