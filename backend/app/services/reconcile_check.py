"""Run the reconciliation as a CHECK, not as a page — and make the answer impossible to miss.

WHY THIS EXISTS
    /api/reconciliation could answer "does the broker hold what the slate says" from the day issue
    #22's first half shipped. Nothing ever asked it. The cycle went on reading
    docs/SLATE.md, debating positions against it and writing a report, for weeks after the account
    of record moved brokers — while the live book matched the document on ZERO of eighteen names.
    A page nobody opens is not a control.

IT WARNS, IT DOES NOT BLOCK
    SENIOR_ENGINEER_BAR §7.2 says "alert/halt on mismatch". The halt is available and is OFF by
    default, because a guardrail that silently stops work is how a cycle quietly does nothing for a
    week and everyone assumes it ran. Desync is loud at ERROR, recorded on the run, and printed at
    the TOP of the report; whether it also stops the run is `cycle_halt_on_desync`, a tunable an
    operator can see and change. Observable and overridable, never silent.

WHY IT CALLS THE ROUTER FUNCTION
    `routers/reconciliation.reconciliation()` is a plain function — the diff, the weights and the
    guardrail checks all live in it. Reimplementing that here to avoid importing a router would put
    two definitions of "drifted" in the codebase, and the second one would be wrong within a month.
    One implementation, two callers, is the trade worth making.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("agentic.reconcile_check")

# Findings are stored on the cycle row and printed in the report. Both have readers with finite
# patience, and a book with two hundred undocumented names would otherwise produce a wall.
_MAX_LISTED = 40


class DesyncHalt(RuntimeError):
    """The book does not match the slate and the operator asked for that to stop the cycle.

    Raised by the CALLER, after it has recorded the verdict — never by run() itself. See run().
    """


def run(*, halt_on_desync: bool | None = None) -> dict[str, Any]:
    """Reconcile the live book against the documented slate.

    Returns a summary safe to store and print. NEVER RAISES — including for the halt case.

    The halt is returned as a flag rather than thrown, so the caller can record the verdict BEFORE
    stopping. The first draft raised here, which meant the one run an operator most wanted
    explained — the one that refused to proceed — was the one that stored nothing about why.
    """
    if halt_on_desync is None:
        halt_on_desync = _halt_setting()

    try:
        from app.routers.reconciliation import reconciliation

        report = reconciliation()
    # Broad on purpose: a failed preflight must not take the cycle with it.
    except Exception as exc:
        logger.error("reconciliation could not run: %s", exc, exc_info=True)
        return {"checked": False, "reason": f"{type(exc).__name__}: {exc}"}

    meta = report.get("meta") or {}
    if meta.get("slate_documented") is False:
        # Not a desync and not a failure — this account was never given targets. Reported as
        # "did not run", with the reason, so the cycle row stores NULLs (024) rather than a
        # verdict about a comparison that did not happen.
        reason = report.get("note") or "no documented slate for this account"
        logger.info("reconciliation skipped: %s", reason)
        return {"checked": False, "reason": reason}

    summary = report.get("summary") or {}
    positions = report.get("positions") or []
    checks = report.get("checks") or []

    breaches = [c for c in checks if c.get("status") == "breach"]
    # Recomputed from the rows rather than trusted from meta.in_sync: the counts are what gets
    # stored and what 024's CHECK constraint compares against, so they must be the same arithmetic.
    drifted = sum(1 for p in positions if p.get("status") == "drifted")
    missing = sum(1 for p in positions if p.get("status") == "missing")
    unexpected = sum(1 for p in positions if p.get("status") == "unexpected")
    matched = sum(1 for p in positions if p.get("status") == "match")
    in_sync = not (drifted or missing or unexpected or breaches)

    result = {
        "checked": True,
        "in_sync": in_sync,
        "matched": matched,
        "drifted": drifted,
        "missing": missing,
        "unexpected": unexpected,
        "breaches": len(breaches),
        "slate_source": meta.get("slate_source"),
        "slate_dated": meta.get("slate_dated"),
        "account_value": meta.get("account_value"),
        "documented_book_value": meta.get("documented_book_value"),
        "live_cash_pct": meta.get("live_cash_pct"),
        "snapshot_stale": meta.get("snapshot_stale"),
        "findings": _findings(positions, breaches),
    }

    _announce(result, summary)

    if not in_sync and halt_on_desync:
        result["halt"] = (
            f"the book does not match {meta.get('slate_source')}: {missing} missing, "
            f"{drifted} drifted, {unexpected} undocumented, {len(breaches)} guardrail breach(es). "
            "cycle_halt_on_desync is on; turn it off to run anyway."
        )
    return result


def _halt_setting() -> bool:
    try:
        from app.services import settings_store

        return bool(settings_store.get_or("cycle_halt_on_desync", 0.0) >= 1.0)
    except Exception as exc:  # noqa: BLE001
        # Default to NOT halting. A settings read that fails must not be the reason a cycle stops —
        # that would make an unrelated database blip look like a portfolio problem.
        logger.warning("could not read cycle_halt_on_desync, not halting: %s", exc)
        return False


def _findings(positions: list[dict], breaches: list[dict]) -> list[dict]:
    """Only what is wrong. A matched name carries no information worth storing."""
    interesting = [p for p in positions if p.get("status") != "match"]
    return [
        *(
            {
                "kind": p.get("status"),
                "symbol": p.get("symbol"),
                "live_weight_pct": p.get("live_weight_pct"),
                "target_weight_pct": p.get("target_weight_pct"),
                "drift_pct": p.get("drift_pct"),
            }
            for p in interesting[:_MAX_LISTED]
        ),
        *(
            {"kind": "breach", "rule": c.get("rule"), "detail": c.get("detail")}
            for c in breaches[:_MAX_LISTED]
        ),
    ]


def _announce(result: dict, summary: dict) -> None:
    """One log line an operator cannot read as routine.

    ERROR, not WARNING: this lands in logs/cron/ every run and the whole point is that the last
    six weeks of it looked exactly like a healthy run.
    """
    if result["in_sync"]:
        logger.info(
            "reconciliation: in sync — %d position(s) match %s",
            result["matched"],
            result["slate_source"],
        )
        return

    logger.error(
        "RECONCILIATION FAILED — the book does not match %s (dated %s): "
        "%d matched, %d drifted, %d missing, %d undocumented, %d guardrail breach(es). "
        "Every debate below reasons from that document.",
        result["slate_source"],
        result["slate_dated"],
        result["matched"],
        result["drifted"],
        result["missing"],
        result["unexpected"],
        result["breaches"],
    )
    if summary and summary != {}:
        logger.debug("reconciliation summary: %s", json.dumps(summary, default=str))


def report_section(result: dict | None) -> list[str]:
    """The reconciliation block for the cycle report. Goes at the TOP, deliberately.

    It is the only section that changes how every other section should be read: a scan and eight
    debates mean something different when the slate they were judged against describes a portfolio
    that does not exist.
    """
    if result is None or not result.get("checked"):
        reason = (result or {}).get("reason", "not run")
        return [
            "## ⚠ Reconciliation — DID NOT RUN",
            f"- {reason}",
            "- Everything below was produced without checking that the slate matches the broker.",
            "",
        ]

    if result["in_sync"]:
        return [
            "## Reconciliation — in sync",
            (
                f"- {result['matched']} position(s) match {result['slate_source']} "
                f"(dated {result['slate_dated']})."
            ),
            "",
        ]

    lines = [
        "## ⚠ Reconciliation — OUT OF SYNC",
        (
            f"- **The book does not match `{result['slate_source']}` (dated "
            f"{result['slate_dated']}).** Everything below reasons from that document."
        ),
        (
            f"- {result['matched']} matched · {result['drifted']} drifted · "
            f"{result['missing']} missing · {result['unexpected']} undocumented · "
            f"{result['breaches']} guardrail breach(es)"
        ),
    ]
    account_value, documented = result.get("account_value"), result.get("documented_book_value")
    if account_value and documented and documented > 0:
        ratio = account_value / documented
        # Flagged separately from the position diff because it is a different KIND of wrong: the
        # positions can drift within a book, but a book value off by a multiple means the document
        # describes a different account entirely.
        if ratio >= 2 or ratio <= 0.5:
            lines.append(
                f"- Account value ${account_value:,.2f} against a documented ${documented:,.2f} — "
                "the slate describes a different book, not a drifted one."
            )
    for finding in result.get("findings", []):
        if finding.get("kind") == "breach":
            lines.append(f"  - breach — {finding['rule']}: {finding['detail']}")
        elif finding.get("kind") == "missing":
            lines.append(
                f"  - missing — {finding['symbol']} (target {finding['target_weight_pct']}%)"
            )
        elif finding.get("kind") == "unexpected":
            lines.append(
                f"  - undocumented — {finding['symbol']} at {finding['live_weight_pct']}%"
            )
        else:
            lines.append(
                f"  - drifted — {finding['symbol']}: {finding['live_weight_pct']}% vs "
                f"{finding['target_weight_pct']}% target ({finding['drift_pct']:+.2f} pp)"
            )
    lines.append("")
    return lines
