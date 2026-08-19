"""Arming, preview and confirmation — the decisions above the submission client.

READ docs/EXECUTION_DESIGN.md FIRST. This module implements it; that document argues for it.

THE SHAPE, AND WHY IT IS TWO CALLS
    ``build_preview`` validates, sizes, refetches account state UNCACHED, evaluates every guardrail
    and returns an artefact. It places nothing. ``confirm`` takes that artefact's id, re-checks what
    can have changed, writes the audit row, and only then submits.

    The preview is the thing an operator approves. Collapsing the two into one call would mean the
    approval and the order are the same act, and there would be nothing to show someone before money
    moves.

WHY ARMING IS SEPARATE FROM LOGIN
    Before this, a session could only read. It can now trade. Arming is a distinct, audited,
    expiring act so that holding a valid session is not by itself sufficient to move money. It is
    the one place in this codebase where friction is the feature.

    Stored in the database rather than in a module global: a process restart must not leave
    execution silently armed, and "who armed this" has to survive the process that recorded it.

WHAT REFUSES, AND IN WHAT ORDER
    Cheapest and most absolute first, so an expensive check never runs for a request that was never
    going to be allowed:
        1. execution disabled in config      -> 403, always, no exceptions
        2. not armed / arming expired        -> 409
        3. rate cap exhausted                -> 429 AND disarms
        4. preview unknown or expired        -> 409
        5. guardrails block without override -> 422
    Every refusal names the rule, the threshold and the observed value. A refusal the operator
    cannot argue with is the failure mode this design exists to avoid.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import DbUnavailable, connection
from app.services import guardrails as gr
from app.services.broker import alpaca_configured

logger = logging.getLogger("agentic.execution")


class ExecutionDisabled(RuntimeError):
    """EXECUTION_ENABLED is false. There is no path, not merely a closed one."""


class NotArmed(RuntimeError):
    """Execution is not armed, or the window expired."""


class RateCapExceeded(RuntimeError):
    """The per-window submission cap is spent. Execution has been disarmed."""


class PreviewNotFound(RuntimeError):
    """Unknown or expired preview. Never silently re-priced — a preview is a moment, not a query."""


class GuardrailBlocked(RuntimeError):
    """Guardrails refused and no valid override was supplied."""

    def __init__(self, verdict: gr.Verdict) -> None:
        super().__init__("guardrails blocked this order")
        self.verdict = verdict


# ── previews ──────────────────────────────────────────────────────────────────────────────────
# In-process with a TTL measured in a couple of minutes. Deliberately not persisted: a preview is a
# snapshot of prices and balances at an instant, and one that survived a restart would be offering
# an operator numbers from before the gap. Losing previews on restart is the correct behaviour, not
# a limitation — the operator simply previews again and sees current figures.
_previews: dict[str, dict[str, Any]] = {}
_preview_lock = threading.Lock()


@dataclass(frozen=True)
class Arming:
    armed_by: int | None
    armed_at: datetime
    expires_at: datetime

    @property
    def seconds_remaining(self) -> int:
        return max(0, int((self.expires_at - datetime.now(timezone.utc)).total_seconds()))


def _require_enabled() -> None:
    settings = get_settings()
    if not settings.execution_enabled:
        raise ExecutionDisabled(
            "execution is disabled (EXECUTION_ENABLED=false). This is the shipped default; "
            "enabling it is a deliberate act by someone who has read docs/EXECUTION_DESIGN.md."
        )
    if not alpaca_configured():
        # Without a broker there is nothing to submit to. Named separately from "disabled" because
        # the fix is entirely different.
        raise ExecutionDisabled("no broker credentials configured; execution has no destination")


def current_arming() -> Arming | None:
    """The live arming window, or None. Expiry is evaluated in SQL against the database clock."""
    try:
        with connection() as conn:
            row = conn.execute(
                "SELECT armed_by, armed_at, expires_at FROM execution_arming "
                "WHERE disarmed_at IS NULL AND expires_at > now() "
                "ORDER BY armed_at DESC LIMIT 1"
            ).fetchone()
    except DbUnavailable:
        # Fail CLOSED. If we cannot read the arming state we cannot know execution is permitted, and
        # an unknown that resolves to "allowed" is how a safety gate becomes decorative.
        logger.error("cannot read arming state; treating execution as DISARMED")
        return None
    if row is None:
        return None
    return Arming(armed_by=row[0], armed_at=row[1], expires_at=row[2])


def arm(operator_id: int) -> Arming:
    """Open an arming window. Audited, expiring, and replacing any window already open."""
    _require_enabled()
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.execution_arm_ttl_seconds)
    with connection() as conn:
        # Close any open window first so there is never more than one live row. Two overlapping
        # windows would make "when does this expire" ambiguous, and the later one would silently
        # extend the earlier.
        conn.execute(
            "UPDATE execution_arming SET disarmed_at = now(), disarmed_by = %s, "
            "disarm_reason = 'superseded' WHERE disarmed_at IS NULL AND expires_at > now()",
            (operator_id,),
        )
        row = conn.execute(
            "INSERT INTO execution_arming (armed_by, expires_at) VALUES (%s, %s) "
            "RETURNING armed_by, armed_at, expires_at",
            (operator_id, expires),
        ).fetchone()
    logger.warning(
        "EXECUTION ARMED by operator %s until %s (%ss)",
        operator_id, expires.isoformat(), settings.execution_arm_ttl_seconds,
    )
    return Arming(armed_by=row[0], armed_at=row[1], expires_at=row[2])


def disarm(operator_id: int | None, reason: str = "manual") -> bool:
    """Close the arming window. Never gated on anything — an emergency control that requires
    confirmation is not one. Returns whether a window was actually open."""
    with connection() as conn:
        cur = conn.execute(
            "UPDATE execution_arming SET disarmed_at = now(), disarmed_by = %s, disarm_reason = %s "
            "WHERE disarmed_at IS NULL AND expires_at > now()",
            (operator_id, reason),
        )
        closed = cur.rowcount > 0
    if closed:
        logger.warning("EXECUTION DISARMED by operator %s (%s)", operator_id, reason)
    return closed


def _require_armed() -> Arming:
    armed = current_arming()
    if armed is None:
        raise NotArmed(
            "execution is not armed. Arming is a separate, expiring act: holding a session is "
            "deliberately not enough to move money."
        )
    return armed


def _check_rate_cap(operator_id: int | None) -> None:
    """Refuse — and DISARM — once the window's submission budget is spent.

    Counts ATTEMPTS, not successes: a loop that fails validation is still a runaway, and a cap that
    only counted fills would let one spin freely.
    """
    settings = get_settings()
    window = timedelta(seconds=settings.execution_rate_window_seconds)
    since = datetime.now(timezone.utc) - window
    with connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM orders WHERE submitted_at >= %s", (since,)
        ).fetchone()[0]
    if n >= settings.execution_max_orders_per_window:
        disarm(operator_id, reason="rate_cap")
        raise RateCapExceeded(
            f"{n} orders submitted in the last {settings.execution_rate_window_seconds}s, at the "
            f"cap of {settings.execution_max_orders_per_window}. Execution has been DISARMED and "
            f"must be re-armed by a human."
        )


def build_preview(
    *,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    limit_price: float | None,
    has_recorded_exit: bool,
    drawdown_pct: float | None = None,
) -> dict[str, Any]:
    """Validate, size, guardrail-check and return the artefact an operator approves.

    Places nothing. Refetches the account UNCACHED: the display caches (5s broker, 45s marks) are
    fine for a dashboard and wrong for sizing, and an order sized against a stale balance is one the
    operator did not actually approve.
    """
    _require_enabled()
    settings = get_settings()

    order_type = order_type.strip().lower()
    if order_type not in settings.execution_order_type_list:
        raise ValueError(
            f"order type {order_type!r} is not permitted; allowed: "
            f"{settings.execution_order_type_list!r} (EXECUTION_ORDER_TYPES)"
        )

    from src.alpaca import AlpacaClient, snapshot_from_alpaca

    client = AlpacaClient()
    if not settings.execution_allow_live:
        client.assert_paper()
    snapshot = snapshot_from_alpaca(client.account(), client.positions())

    account_value = float(snapshot["account"]["total_value"])
    cash = float(snapshot["account"]["cash"])
    positions = {
        p["symbol"]: float(p["quantity"]) * float(p["average_buy_price"])
        for p in snapshot["positions"]
    }

    # A limit order prices itself; a market order is priced for ESTIMATION only, and the preview
    # must say so rather than present an estimate as the price the operator will get.
    price = limit_price
    price_is_estimate = False
    if price is None:
        from app.services.marks import get_marks, resolve_ttl_seconds

        price = get_marks([symbol], resolve_ttl_seconds(settings.marks_ttl_seconds)).get(symbol)
        price_is_estimate = True
        if price is None:
            raise ValueError(f"cannot price {symbol}: no live mark available to estimate notional")

    verdict = gr.evaluate(
        side=side, symbol=symbol, qty=qty, price=float(price),
        account_value=account_value, cash=cash, positions=positions,
        settings=settings, has_recorded_exit=has_recorded_exit, drawdown_pct=drawdown_pct,
    )

    preview_id = f"prev-{secrets.token_urlsafe(16)}"
    notional = qty * float(price)
    preview = {
        "preview_id": preview_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_in_s": settings.execution_preview_ttl_seconds,
        "broker_env": snapshot["source"],
        "account_masked": snapshot["account"]["number_masked"],
        "symbol": symbol, "side": side, "order_type": order_type,
        "qty": qty,
        "limit_price": limit_price,
        # Both units, always. Shares-versus-dollars is the confusion that turns a $25 position into
        # a $25,000 one, and the operator should never have to do the multiplication themselves.
        "estimated_notional": round(notional, 2),
        "price_used": float(price),
        "price_is_estimate": price_is_estimate,
        "account_value": account_value,
        "cash_before": cash,
        "cash_after": round(cash - notional if side == "buy" else cash + notional, 2),
        "resulting_position_pct": round(
            ((positions.get(symbol, 0.0) + notional) / account_value * 100.0)
            if account_value > 0 and side == "buy"
            else (positions.get(symbol, 0.0) / account_value * 100.0 if account_value > 0 else 0.0),
            2,
        ),
        # EVERY rule, passes included. "Nothing blocked this" must be something the operator can
        # see, not an absence they have to trust.
        "guardrails": [
            {
                "rule_key": f.rule_key, "severity": f.severity, "message": f.message,
                "threshold": f.threshold, "observed": f.observed, "overridable": f.overridable,
            }
            for f in verdict.findings
        ],
        "blocked": verdict.blocked,
        "requires_override": [f.rule_key for f in verdict.blocking_findings if f.overridable],
        "unoverridable_blocks": [f.rule_key for f in verdict.unoverridable],
    }

    with _preview_lock:
        _previews[preview_id] = {"preview": preview, "created_monotonic": time.monotonic()}
    return preview


def _take_preview(preview_id: str) -> dict[str, Any]:
    settings = get_settings()
    with _preview_lock:
        entry = _previews.get(preview_id)
        if entry is None:
            raise PreviewNotFound(
                f"preview {preview_id!r} is unknown or already used. Previews are single-use and "
                f"short-lived; build a new one to see current prices and balances."
            )
        age = time.monotonic() - entry["created_monotonic"]
        if age > settings.execution_preview_ttl_seconds:
            del _previews[preview_id]
            raise PreviewNotFound(
                f"preview {preview_id!r} expired after {age:.0f}s (limit "
                f"{settings.execution_preview_ttl_seconds}s). It is refused rather than re-priced: "
                f"confirming it would submit an order against numbers the operator never saw."
            )
        # Single-use: removed on take, so a double-confirm cannot reach the broker twice even before
        # client_order_id gets a chance to collide.
        del _previews[preview_id]
    return entry["preview"]


def confirm(
    *,
    preview_id: str,
    operator_id: int,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Submit the order an operator approved. The only function here that can move money.

    ORDER OF OPERATIONS, AND WHY IT IS THIS ONE
        1. enabled → armed → rate cap → preview. Cheapest and most absolute refusals first.
        2. Guardrails re-checked from the preview's recorded verdict. A block needs an override with
           a written reason; an UNOVERRIDABLE block needs no reason because nothing clears it.
        3. The audit row is INSERTed **before** the broker call, with submit_status='submitting'.
           This is the ordering that matters most in the module: an order that vanishes between the
           request and the response still leaves evidence it was attempted. Writing the row after a
           successful submission would mean the only orders on record are the ones that came back.
        4. Submit. Then UPDATE the row with what actually happened.

    A timeout leaves submit_status='unknown', which is a real state and not a synonym for failure.
    Resolving it is reconciliation's job, keyed on client_order_id.
    """
    _require_enabled()
    _require_armed()
    _check_rate_cap(operator_id)
    preview = _take_preview(preview_id)

    settings = get_settings()

    # Guardrails, from the verdict the operator actually saw. Re-evaluating here against fresh
    # numbers would silently change the terms of what was approved — the preview TTL is what keeps
    # those numbers honest, and it is short for exactly this reason.
    unoverridable = preview.get("unoverridable_blocks") or []
    if unoverridable:
        raise GuardrailBlocked(gr.Verdict(findings=[
            gr.Finding(rule_key=k, severity="block", message="not overridable", overridable=False)
            for k in unoverridable
        ]))
    needs_override = preview.get("requires_override") or []
    if needs_override and not (override_reason and len(override_reason.strip()) >= 8):
        raise GuardrailBlocked(gr.Verdict(findings=[
            gr.Finding(
                rule_key=k, severity="block",
                message="blocked; an override requires a written reason of at least 8 characters",
            )
            for k in needs_override
        ]))

    # Derived from the preview, never generated at send time: a retry after an ambiguous timeout
    # must carry the SAME id so it collides instead of duplicating.
    client_order_id = f"ww-{preview_id}"

    from src.alpaca import AlpacaClient
    from src.alpaca_execution import (
        ExecutionRefused,
        SubmissionUncertain,
        submit_order,
    )

    client = AlpacaClient()

    with connection() as conn:
        order_row_id = conn.execute(
            "INSERT INTO orders ("
            " client_order_id, preview_id, preview, operator_id, broker_env, account_masked,"
            " symbol, side, order_type, time_in_force, requested_qty, limit_price,"
            " guardrails_passed, override_by, override_reason, submit_status"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'submitting') RETURNING id",
            (
                client_order_id, preview_id, json.dumps(preview), operator_id,
                preview["broker_env"], preview["account_masked"],
                preview["symbol"], preview["side"], preview["order_type"], "day",
                preview["qty"], preview["limit_price"],
                not preview["blocked"],
                operator_id if needs_override else None,
                override_reason.strip() if needs_override and override_reason else None,
            ),
        ).fetchone()[0]

    def _finish(**fields: Any) -> None:
        sets = ", ".join(f"{k} = %s" for k in fields)
        with connection() as conn:
            conn.execute(
                f"UPDATE orders SET {sets}, updated_at = now() WHERE id = %s",
                (*fields.values(), order_row_id),
            )

    try:
        order = submit_order(
            client=client,
            client_order_id=client_order_id,
            symbol=preview["symbol"],
            side=preview["side"],
            qty=float(preview["qty"]),
            order_type=preview["order_type"],
            limit_price=preview["limit_price"],
            allowed_types=settings.execution_order_type_list,
            allow_live=settings.execution_allow_live,
        )
    except SubmissionUncertain as exc:
        # NOT rejected. Nobody knows. Recorded as such so reconciliation resolves it and nothing
        # resubmits in the meantime.
        _finish(submit_status="unknown", submit_error=str(exc)[:500])
        logger.error("ORDER OUTCOME UNKNOWN id=%s client_order_id=%s: %s",
                     order_row_id, client_order_id, exc)
        raise
    except ExecutionRefused as exc:
        _finish(submit_status="rejected", submit_error=str(exc)[:500])
        raise
    except Exception as exc:
        _finish(submit_status="unknown", submit_error=str(exc)[:500])
        logger.exception("ORDER SUBMISSION FAILED id=%s client_order_id=%s", order_row_id, client_order_id)
        raise

    _finish(
        submit_status="accepted",
        broker_order_id=str(order.get("id") or "")[:128] or None,
        broker_status=order.get("status"),
    )
    return {
        "order_id": order_row_id,
        "client_order_id": client_order_id,
        "broker_order_id": order.get("id"),
        "status": order.get("status"),
        "symbol": preview["symbol"],
        "side": preview["side"],
        "qty": preview["qty"],
        "broker_env": preview["broker_env"],
    }
