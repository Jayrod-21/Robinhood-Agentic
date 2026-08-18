"""GET /api/reconciliation — what the broker holds versus what the slate says it should.

Contract: docs/contracts/reconciliation-endpoint.md. Read-only. Issues #22 and #2.

THE QUESTION NOBODY COULD ANSWER
    The documented slate lives in a markdown file; the holdings live at a broker. Nothing compared
    them, so a position could drift, be sold, or never be bought at all and the written plan would
    go on looking authoritative. That is issue #22 in one sentence, and it has been open since the
    slate was written.

WHAT "DRIFT" MEANS HERE, PRECISELY
    Weights are a share of ACCOUNT value (positions + cash), not of equity alone. Both numbers are
    defensible, but the slate's own table includes a CASH row summing to 100% — so account-value
    basis is the one that matches the document being reconciled against. Reconciling against a
    different denominator than the plan was written in would produce drift that is an artefact of
    arithmetic rather than a fact about the book.

    drift_pct = live_weight_pct - target_weight_pct, in percentage POINTS.

STATUS, AND WHY 'UNEXPECTED' IS NOT AN ERROR
    match | drifted | missing | unexpected. A held name the slate does not document is 'unexpected'
    — a finding to surface, not a fault. Some of the most useful rows this endpoint can produce are
    positions nobody wrote down.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.services.broker import get_snapshot
from app.services.marks import get_marks
from app.services.slate import load_sizing_rules, load_slate
from app.services.snapshot import SnapshotError

logger = logging.getLogger("agentic.api.reconciliation")

router = APIRouter(prefix="/api", tags=["reconciliation"])

# Percentage POINTS of drift before a held name counts as drifted rather than matched.
#
# 1.5 is the contract's suggestion and it is kept, but it is not scale-free: on a 22% target that is
# 7% relative, on a 2% target it is 75%. Both drift_pct and drift_rel_pct are returned so the page
# can show either, and the threshold is reported in `meta` so the UI states it rather than hardcodes
# a second copy that can drift from this one.
DRIFT_TOLERANCE_PCT = 1.5

# Cash band and off-factor floor come from the charter and SLATE.md respectively.
CASH_BAND = (10.0, 20.0)
OFF_FACTOR_NAMES = ("V", "CVX")
OFF_FACTOR_FLOOR_PCT = 20.0
MAX_POSITION_PCT = 25.0

_SLATE_DATED = re.compile(r"Allocation \(as of (?P<date>\d{4}-\d{2}-\d{2})\)")
# "$100 Agentic account" — what the slate assumed the book was worth when it was written. A gap
# against the live account value usually means deposits nobody recorded.
_DOCUMENTED_BOOK = re.compile(r"\$(?P<amount>[0-9][0-9,]*(?:\.[0-9]{2})?)\s+Agentic account")


def _docs_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[3] / "docs"


def _slate_meta(text: str) -> tuple[str | None, float | None]:
    dated = None
    m = _SLATE_DATED.search(text)
    if m:
        dated = m.group("date")
    book = None
    m = _DOCUMENTED_BOOK.search(text)
    if m:
        book = float(m.group("amount").replace(",", ""))
    return dated, book


@router.get("/reconciliation")
def reconciliation() -> dict[str, Any]:
    settings = get_settings()
    docs = _docs_dir()
    slate_path = docs / "SLATE.md"
    slate = load_slate(slate_path)
    rules = load_sizing_rules(slate_path)

    if not slate:
        # 503, not an empty reconciliation. "The slate did not parse" and "the broker holds nothing
        # the slate documents" are opposite conclusions, and rendering the first as the second would
        # report a parser failure as a portfolio finding.
        raise HTTPException(
            status_code=503,
            detail="The documented slate could not be read, so there is nothing to reconcile against.",
        )

    try:
        snapshot = get_snapshot(settings.snapshot_path)
    except SnapshotError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    try:
        slate_text = slate_path.read_text(encoding="utf-8")
    except OSError:
        slate_text = ""
    slate_dated, documented_book = _slate_meta(slate_text)

    symbols = [p.symbol for p in snapshot.positions]
    marks = get_marks(symbols, settings.marks_ttl_seconds) if symbols else {}
    account_value = snapshot.account.total_value
    cash = snapshot.account.cash

    held: dict[str, dict[str, Any]] = {}
    for p in snapshot.positions:
        price = marks.get(p.symbol)
        market_value = p.quantity * price if price is not None else None
        cost_basis = p.quantity * p.average_buy_price
        held[p.symbol] = {
            "market_value": market_value,
            "weight": (market_value / account_value * 100.0)
            if market_value is not None and account_value
            else None,
            "unrealized_pl_pct": (
                ((market_value - cost_basis) / cost_basis * 100.0)
                if market_value is not None and cost_basis
                else None
            ),
            "priced": price is not None,
        }

    rows: list[dict[str, Any]] = []
    counts = {"match": 0, "drifted": 0, "missing": 0, "unexpected": 0}

    for ticker, entry in sorted(slate.items(), key=lambda kv: -kv[1].target_weight_pct):
        h = held.get(ticker)
        live_weight = h["weight"] if h else None
        drift = round(live_weight - entry.target_weight_pct, 2) if live_weight is not None else None
        if h is None:
            status = "missing"
        elif live_weight is None:
            # Held but unpriced: the drift is unknown, and calling that "match" would assert
            # agreement nobody measured.
            status = "drifted"
        elif abs(drift) <= DRIFT_TOLERANCE_PCT:
            status = "match"
        else:
            status = "drifted"
        counts[status] += 1
        rows.append({
            "symbol": ticker,
            "target_weight_pct": entry.target_weight_pct,
            "live_weight_pct": round(live_weight, 2) if live_weight is not None else None,
            "drift_pct": drift,
            "drift_rel_pct": (
                round(drift / entry.target_weight_pct * 100.0, 1)
                if drift is not None and entry.target_weight_pct
                else None
            ),
            "status": status,
            "market_value": round(h["market_value"], 2) if h and h["market_value"] is not None else None,
            "unrealized_pl_pct": round(h["unrealized_pl_pct"], 2) if h and h["unrealized_pl_pct"] is not None else None,
            "priced": h["priced"] if h else None,
            "in_universe": True,
            "role": entry.role,
            "note": None if h else "documented in the slate, not held at the broker",
        })

    for ticker in sorted(set(held) - set(slate)):
        h = held[ticker]
        counts["unexpected"] += 1
        rows.append({
            "symbol": ticker,
            "target_weight_pct": None,
            "live_weight_pct": round(h["weight"], 2) if h["weight"] is not None else None,
            "drift_pct": None,
            "drift_rel_pct": None,
            "status": "unexpected",
            "market_value": round(h["market_value"], 2) if h["market_value"] is not None else None,
            "unrealized_pl_pct": round(h["unrealized_pl_pct"], 2) if h["unrealized_pl_pct"] is not None else None,
            "priced": h["priced"],
            "in_universe": False,
            "role": None,
            "note": "held at the broker, not documented in the slate",
        })

    live_cash_pct = (cash / account_value * 100.0) if account_value else 0.0
    checks = _run_checks(rows=rows, live_cash_pct=live_cash_pct, rules=rules)

    generated = snapshot.generated_at
    stale = True
    try:
        gen = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        stale = (datetime.now(timezone.utc) - gen).total_seconds() > settings.snapshot_max_age_seconds
    except (ValueError, AttributeError):
        pass

    return {
        "meta": {
            "slate_source": "docs/SLATE.md",
            "slate_dated": slate_dated,
            "snapshot_generated_at": generated,
            "snapshot_stale": stale,
            "source": snapshot.source,
            "account_value": round(account_value, 2),
            # A gap against the live account value is usually unrecorded deposits — which is why the
            # slate's own assumed book size is reported rather than quietly ignored.
            "documented_book_value": documented_book,
            "target_cash_pct": CASH_BAND[0],
            "live_cash_pct": round(live_cash_pct, 2),
            "drift_tolerance_pct": DRIFT_TOLERANCE_PCT,
            "in_sync": counts["drifted"] == 0 and counts["missing"] == 0 and counts["unexpected"] == 0,
        },
        "positions": rows,
        "checks": checks,
        "summary": {
            "matched": counts["match"],
            "drifted": counts["drifted"],
            "missing": counts["missing"],
            "unexpected": counts["unexpected"],
            "checks_total": len(checks),
            "checks_failing": sum(1 for c in checks if c["status"] == "breach"),
        },
    }


def _run_checks(*, rows: list[dict[str, Any]], live_cash_pct: float, rules) -> list[dict[str, Any]]:
    """The charter and slate rules, each as a row the page can render.

    Every rule reports whether it PASSED as well as whether it failed — the same reasoning as the
    guardrail evaluator: a rule that only appears when broken leaves "was this even checked?"
    unanswerable.
    """
    checks: list[dict[str, Any]] = []

    breached = [
        r for r in rows
        if r["unrealized_pl_pct"] is not None and r["unrealized_pl_pct"] <= rules.hard_stop_pct
    ]
    near = [
        r for r in rows
        if r["unrealized_pl_pct"] is not None
        and rules.hard_stop_pct < r["unrealized_pl_pct"] <= rules.hard_stop_pct + 5
    ]
    checks.append({
        "rule": f"Hard stop {rules.hard_stop_pct:.0f}% per name",
        "source": "SLATE.md §Sizing discipline",
        "status": "breach" if breached else "pass",
        "severity": "alert" if breached else "info",
        "detail": (
            "; ".join(f"{r['symbol']} {r['unrealized_pl_pct']}% has breached" for r in breached)
            + ("; " if breached and near else "")
            + "; ".join(f"{r['symbol']} {r['unrealized_pl_pct']}% is near" for r in near)
        ) or "no held name is at or past its stop",
    })

    over = [r for r in rows if r["live_weight_pct"] is not None and r["live_weight_pct"] > MAX_POSITION_PCT]
    checks.append({
        "rule": f"Max ~{MAX_POSITION_PCT:.0f}% per name",
        "source": "AGENTIC_ROBINHOOD_v1.md §5",
        "status": "breach" if over else "pass",
        "severity": "alert" if over else "info",
        "detail": "; ".join(f"{r['symbol']} at {r['live_weight_pct']}%" for r in over)
        or f"no name exceeds {MAX_POSITION_PCT:.0f}% of account value",
    })

    lo, hi = CASH_BAND
    cash_ok = lo <= live_cash_pct <= hi
    checks.append({
        "rule": f"Cash {lo:.0f}-{hi:.0f}% band",
        "source": "AGENTIC_ROBINHOOD_v1.md §5",
        "status": "pass" if cash_ok else "breach",
        "severity": "info" if cash_ok else "warn",
        "detail": f"cash is {live_cash_pct:.1f}% of account value"
        + ("" if cash_ok else f", outside the {lo:.0f}-{hi:.0f}% band"),
    })

    off_factor = sum(
        r["live_weight_pct"] or 0.0 for r in rows if r["symbol"] in OFF_FACTOR_NAMES
    )
    checks.append({
        "rule": f"Off-factor floor {'+'.join(OFF_FACTOR_NAMES)} >= {OFF_FACTOR_FLOOR_PCT:.0f}%",
        "source": "SLATE.md §Sizing discipline",
        "status": "pass" if off_factor >= OFF_FACTOR_FLOOR_PCT else "breach",
        "severity": "info" if off_factor >= OFF_FACTOR_FLOOR_PCT else "warn",
        "detail": f"{'+'.join(OFF_FACTOR_NAMES)} total {off_factor:.1f}% of account value",
    })

    return checks
