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
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.services import accounts, settings_store
from app.services.broker import get_snapshot
from app.services.freshness import is_stale
from app.services.marks import get_marks, resolve_ttl_seconds
from app.services.slate import (
    SLATE_DIR,
    load_sizing_rules,
    load_slate,
    slate_path_for,
    slate_status,
)
from app.services.snapshot import SnapshotError
from app.services.valuation import account_totals, value_position, weight_pct

logger = logging.getLogger("agentic.api.reconciliation")

AccountId = Annotated[
    int | None,
    Query(ge=1, le=9, description="which configured account to read; omitted means the default"),
]

router = APIRouter(prefix="/api", tags=["reconciliation"])

# THRESHOLDS ARE TUNABLE, AND THIS ROUTE STILL WORKS WITHOUT THE DATABASE.
#
# These four used to be module constants, so moving a guardrail meant editing Python and
# redeploying — which in practice meant they never moved, and a guardrail nobody can tune is one
# that gets ignored rather than adjusted. They now live in app_settings (migration 019).
#
# But this route had NO database dependency: it reads a broker snapshot and a markdown file. Making
# it require Postgres would mean a database outage takes down the page that tells you whether your
# book matches your plan, while both of that page's actual inputs are still perfectly readable. So
# settings_store falls back to the compiled defaults and REPORTS which it used; `meta.thresholds_source`
# carries that to the UI. A breach judged against a default the operator did not choose is a
# guardrail misreporting itself, so it is never silent.
#
# Drift is not scale-free: 1.5 points on a 22% target is 7% relative, on a 2% target it is 75%.
# Both drift_pct and drift_rel_pct are returned so the page can show either.
OFF_FACTOR_NAMES = ("V", "CVX")

_SLATE_DATED = re.compile(r"Allocation \(as of (?P<date>\d{4}-\d{2}-\d{2})\)")
# "$100 Agentic account" — what the slate assumed the book was worth when it was written. A gap
# against the live account value usually means deposits nobody recorded.
# The book size the slate was written against. TWO patterns, in order:
#
#   1. An explicit `Documented book: $100,000` line. This is what a slate should carry.
#   2. The original prose form, "$100,000 Agentic account", kept so slates written before the
#      labelled line existed still parse.
#
# Pattern 2 is why this comment is long. It scraped a number out of an English sentence, so
# rewriting that sentence — which happened the moment SLATE.md gained a per-account header —
# silently turned documented_book_value into None, and the endpoint went on reporting a book size
# of "unknown" as though the slate had never claimed one. A value that depends on prose phrasing is
# a value that breaks on an edit nobody thought was risky.
_DOCUMENTED_BOOK_LABELLED = re.compile(
    r"Documented book:\s*\$(?P<amount>[0-9][0-9,]*(?:\.[0-9]{2})?)", re.IGNORECASE
)
_DOCUMENTED_BOOK = re.compile(r"\$(?P<amount>[0-9][0-9,]*(?:\.[0-9]{2})?)\s+Agentic account")





def _slate_meta(text: str) -> tuple[str | None, float | None]:
    dated = None
    m = _SLATE_DATED.search(text)
    if m:
        dated = m.group("date")
    book = None
    m = _DOCUMENTED_BOOK_LABELLED.search(text) or _DOCUMENTED_BOOK.search(text)
    if m:
        book = float(m.group("amount").replace(",", ""))
    return dated, book


def _relative(path: Any, settings: Any) -> str:
    """The slate's path as it is written in the docs, not as it is mounted in a container."""
    try:
        return f"docs/{path.relative_to(settings.docs_dir)}"
    except (ValueError, AttributeError):
        return str(path)


def _undocumented(account_id: int | None, settings: Any, docs: Any) -> dict[str, Any]:
    """The response for an account with no slate on file.

    Deliberately NOT a diff. Listing every holding as 'unexpected' against an empty target set is
    technically true and practically a lie: it reads as fifteen findings when the finding is one,
    and it is the shape that trains an operator to skim past a reconciliation panel.
    """
    resolved = account_id or accounts.DEFAULT_ACCOUNT_ID
    expected = f"docs/{SLATE_DIR}/account-{resolved}.md"
    logger.info("account %s has no documented slate (looked for %s)", resolved, expected)
    return {
        "meta": {
            "slate_source": None,
            "slate_documented": False,
            "account_id": resolved,
            "slate_dated": None,
            "expected_slate_path": expected,
            "thresholds_source": None,
        },
        "summary": {"matched": 0, "drifted": 0, "missing": 0, "unexpected": 0,
                    "checks_total": 0, "checks_failing": 0},
        "positions": [],
        "checks": [],
        "note": (
            f"Account {resolved} has no documented slate. Nothing was reconciled — this is a "
            f"normal state for a testing book. Write {expected} to give it targets."
        ),
    }


def _retired(account_id: int | None, slate_path: Any, reason: str | None, settings: Any) -> dict[str, Any]:
    """The response for an account whose slate exists but no longer governs the book.

    Shaped like :func:`_undocumented` for the same reason: the finding is "no targets are in force",
    which is ONE fact, and rendering it as fifteen unexpected holdings is the shape that teaches an
    operator to skim. It differs in naming the document, because "there is no slate" and "the slate
    was retired on purpose, and here is why" are different states and an operator must be able to
    tell which one they are in.
    """
    resolved = account_id or accounts.DEFAULT_ACCOUNT_ID
    source = _relative(slate_path, settings)
    logger.info("account %s has no slate in force (%s is marked NOT IN FORCE)", resolved, source)
    note = (
        f"Account {resolved} has no slate in force. `{source}` is retained as the written record of "
        f"the debate that produced it, but is marked NOT IN FORCE and was not applied to the book."
    )
    if reason:
        note = f"{note} Reason given: {reason}"
    return {
        "meta": {
            # Named, unlike the no-file case: the document exists and an operator should be able to
            # open the thing that is not governing them.
            "slate_source": source,
            "slate_documented": True,
            "slate_in_force": False,
            "slate_retired_reason": reason,
            "account_id": resolved,
            "slate_dated": None,
            "expected_slate_path": source,
            "thresholds_source": None,
        },
        "summary": {"matched": 0, "drifted": 0, "missing": 0, "unexpected": 0,
                    "checks_total": 0, "checks_failing": 0},
        "positions": [],
        "checks": [],
        "note": note,
    }


@router.get("/reconciliation")
def reconciliation(account_id: AccountId = None) -> dict[str, Any]:
    settings = get_settings()
    docs = settings.docs_dir
    # Per ACCOUNT, never a fall-back to account 1's plan. A slate is a claim about what a specific
    # book should hold, and applying one account's claim to another's holdings does not produce a
    # weaker answer — it produces a wrong one, on every row.
    slate_path = slate_path_for(docs, account_id, accounts.DEFAULT_ACCOUNT_ID)

    if slate_path is None:
        # 200 with an explicit "nothing to reconcile against". NOT a 503, which means the slate
        # could not be READ, and not a diff against some other account's targets. An account with
        # no documented slate is a normal state — a testing book is not supposed to have one.
        return _undocumented(account_id, settings, docs)

    # A slate on disk is not automatically a slate in force. Checked BEFORE parsing the table, so a
    # retired document is reported as retired even if its format has since rotted — the reason an
    # operator gets should be "this was retired", never a parser error about a file that stopped
    # mattering weeks ago.
    status = slate_status(slate_path)
    if not status.in_force:
        return _retired(account_id, slate_path, status.reason, settings)

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
        snapshot = get_snapshot(settings.snapshot_path, account_id)
    except SnapshotError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    try:
        slate_text = slate_path.read_text(encoding="utf-8")
    except OSError:
        slate_text = ""
    slate_dated, documented_book = _slate_meta(slate_text)

    tunables, thresholds_source = settings_store.get_all()
    drift_tolerance = tunables["drift_tolerance_pct"]
    cash_band = (tunables["cash_floor_pct"], tunables["cash_ceiling_pct"])
    max_position = tunables["max_position_pct"]
    off_factor_floor = tunables["off_factor_floor_pct"]

    symbols = [p.symbol for p in snapshot.positions]
    marks = get_marks(symbols, resolve_ttl_seconds(settings.marks_ttl_seconds)) if symbols else {}
    cash = snapshot.account.cash
    values = [
        value_position(p.quantity, p.average_buy_price, marks.get(p.symbol))
        for p in snapshot.positions
    ]
    totals = account_totals(values, cash)

    # The SAME denominator the Portfolio page uses: priced market value + cash, both from our own
    # marks. This used snapshot.account.total_value — the broker's figure, computed from the
    # broker's prices — while every numerator was an FMP mark. Two price sources in one ratio, so
    # the same position could show one weight here and another on Portfolio, with nothing on either
    # page saying which basis it was. See services/valuation.py for why this basis and not that one.
    account_value = totals.total_value

    held: dict[str, dict[str, Any]] = {
        p.symbol: {
            "market_value": v.market_value,
            "weight": weight_pct(v.market_value, account_value),
            "unrealized_pl_pct": v.unrealized_pl_pct,
            "priced": v.priced,
        }
        for p, v in zip(snapshot.positions, values, strict=True)
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
        elif abs(drift) <= drift_tolerance:
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
    checks = _run_checks(
        rows=rows, live_cash_pct=live_cash_pct, rules=rules,
        cash_band=cash_band, max_position=max_position, off_factor_floor=off_factor_floor,
    )

    generated = snapshot.generated_at
    stale = is_stale(generated, settings.snapshot_max_age_seconds, field="snapshot generated_at")

    return {
        "meta": {
            "slate_source": _relative(slate_path, settings),
            "slate_documented": True,
            "account_id": account_id or accounts.DEFAULT_ACCOUNT_ID,
            "slate_dated": slate_dated,
            "snapshot_generated_at": generated,
            "snapshot_stale": stale,
            "source": snapshot.source,
            "account_value": round(account_value, 2),
            # A gap against the live account value is usually unrecorded deposits — which is why the
            # slate's own assumed book size is reported rather than quietly ignored.
            "documented_book_value": documented_book,
            "target_cash_pct": cash_band[0],
            "live_cash_pct": round(live_cash_pct, 2),
            "drift_tolerance_pct": drift_tolerance,
            # "defaults" means the database could not be read and every threshold above is the
            # compiled default — NOT that the operator chose them.
            "thresholds_source": thresholds_source,
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


def _run_checks(
    *, rows: list[dict[str, Any]], live_cash_pct: float, rules,
    cash_band: tuple[float, float], max_position: float, off_factor_floor: float,
) -> list[dict[str, Any]]:
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

    over = [r for r in rows if r["live_weight_pct"] is not None and r["live_weight_pct"] > max_position]
    checks.append({
        "rule": f"Max ~{max_position:.0f}% per name",
        "source": "AGENTIC_ROBINHOOD_v1.md §5",
        "status": "breach" if over else "pass",
        "severity": "alert" if over else "info",
        "detail": "; ".join(f"{r['symbol']} at {r['live_weight_pct']}%" for r in over)
        or f"no name exceeds {max_position:.0f}% of account value",
    })

    lo, hi = cash_band
    cash_ok = lo <= live_cash_pct <= hi
    checks.append({
        "rule": f"Cash {lo:.0f}-{hi:.0f}% band",
        "source": "AGENTIC_ROBINHOOD_v1.md §5",
        "status": "pass" if cash_ok else "breach",
        "severity": "info" if cash_ok else "warn",
        "detail": f"cash is {live_cash_pct:.1f}% of account value"
        + ("" if cash_ok else f", outside the {lo:.0f}-{hi:.0f}% band"),
    })

    # An off-factor name that is HELD but unpriced makes this check unanswerable, not failed.
    #
    # `r["live_weight_pct"] or 0.0` turned an unknown weight into zero, so a transient pricing gap
    # on V or CVX flipped "V+CVX >= 20%" to breach while both were held at full weight — a guardrail
    # firing on a data outage and pointing at the portfolio. Everywhere else this route treats
    # unknown as unknown (an unpriced holding is "drifted", never "match"); this one check did the
    # opposite and resolved unknown to the worst case.
    off_rows = [r for r in rows if r["symbol"] in OFF_FACTOR_NAMES and r["status"] != "missing"]
    unpriced = [r["symbol"] for r in off_rows if r["live_weight_pct"] is None]
    off_factor = sum(r["live_weight_pct"] or 0.0 for r in off_rows)
    rule = f"Off-factor floor {'+'.join(OFF_FACTOR_NAMES)} >= {off_factor_floor:.0f}%"

    if unpriced:
        checks.append({
            "rule": rule,
            "source": "SLATE.md §Sizing discipline",
            "status": "unknown",
            "severity": "warn",
            "detail": (
                f"cannot be checked: {', '.join(unpriced)} held but unpriced, so the off-factor "
                f"total is unknown rather than {off_factor:.1f}%"
            ),
        })
    else:
        checks.append({
            "rule": rule,
            "source": "SLATE.md §Sizing discipline",
            "status": "pass" if off_factor >= off_factor_floor else "breach",
            "severity": "info" if off_factor >= off_factor_floor else "warn",
            "detail": f"{'+'.join(OFF_FACTOR_NAMES)} total {off_factor:.1f}% of account value",
        })

    return checks
