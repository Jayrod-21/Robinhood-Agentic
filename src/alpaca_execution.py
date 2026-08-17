"""Order submission to Alpaca — the ONLY module in this codebase that can move money.

SEPARATE FROM src/alpaca.py, DELIBERATELY
    That module's docstring promises "It does not place orders. This module reads." A `place_order`
    appended there would erode a read-only guarantee without anyone deciding to erode it — the
    guarantee would still be written down, and would simply stop being true. So this lives apart,
    under a name that says what it does, and importing it is a visible act in a diff.

WHAT THIS MODULE DOES AND DOES NOT DECIDE
    It submits an order that something else has already validated, sized, guardrail-checked and had
    confirmed by an operator. It makes exactly three decisions of its own, all refusals:

      * it refuses to submit against a live endpoint unless live is explicitly allowed;
      * it refuses an order type outside the configured allow-list;
      * it refuses to invent a client_order_id.

    Everything else — is this a good trade, can the account afford it, has an owner approved it —
    belongs upstream in services/execution.py. A module that both decides and submits is one where
    the decision can be skipped.

THE TIMEOUT PROBLEM, WHICH IS THE WHOLE REASON FOR client_order_id
    A submission can time out after Alpaca accepted it. The client then knows nothing: retrying may
    duplicate the position, not retrying may abandon a real order. There is no safe guess.

    So the caller supplies a client_order_id derived from the preview, and a retry carries the SAME
    id. Alpaca rejects the duplicate, `orders.client_order_id` is UNIQUE locally, and the retry
    becomes a no-op instead of a second position. :class:`SubmissionUncertain` is raised rather than
    a generic error so the caller can record `submit_status='unknown'` — a real state, distinct from
    'rejected', which only reconciliation can resolve.
"""

from __future__ import annotations

import logging
from typing import Any

from src.alpaca import AlpacaClient, AlpacaError

logger = logging.getLogger("agentic.alpaca.execution")

ORDERS_PATH = "/v2/orders"


class ExecutionRefused(RuntimeError):
    """This module declined to submit. No order was sent."""


class SubmissionUncertain(RuntimeError):
    """The request left the process and its outcome is unknown.

    NOT an error meaning "it failed" — an error meaning "nobody knows". The caller must record it as
    such and let reconciliation decide, because treating it as failure invites a retry that
    duplicates, and treating it as success records a fill that may not exist.
    """


def submit_order(
    *,
    client: AlpacaClient,
    client_order_id: str,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    time_in_force: str = "day",
    limit_price: float | None = None,
    allowed_types: list[str],
    allow_live: bool = False,
) -> dict[str, Any]:
    """Submit one order. Returns Alpaca's order object.

    Raises :class:`ExecutionRefused` before anything leaves the process, and
    :class:`SubmissionUncertain` when the request may or may not have landed.
    """
    if not allow_live:
        # The URL routes the request, so the URL is what gets checked — never the key's prefix. A
        # string comparison already mislabelled a paper endpoint as live once (src/alpaca.py's /v2
        # normalisation); the consequence there was a wrong label, here it would be a real order
        # against real money.
        client.assert_paper()

    order_type = order_type.strip().lower()
    if order_type not in allowed_types:
        raise ExecutionRefused(
            f"order type {order_type!r} is not in the configured allow-list {allowed_types!r}. "
            f"Widen EXECUTION_ORDER_TYPES deliberately if that is intended."
        )
    if order_type == "limit" and (limit_price is None or limit_price <= 0):
        raise ExecutionRefused("a limit order requires a positive limit price")
    if order_type == "market" and limit_price is not None:
        raise ExecutionRefused("a market order must not carry a limit price")
    if side not in ("buy", "sell"):
        raise ExecutionRefused(f"side must be buy or sell, got {side!r}")
    if qty <= 0:
        raise ExecutionRefused(f"quantity must be positive, got {qty!r}")
    if not client_order_id:
        # Generating one here would defeat the entire idempotency design: a retry would arrive with
        # a fresh id and Alpaca would accept it as a new order.
        raise ExecutionRefused("client_order_id is required and is never generated here")

    payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        # Shares, as a string — Alpaca's API is string-typed for numerics, and letting a float format
        # itself (1e-05, 10.000000000000002) is a class of bug worth not having.
        "qty": f"{qty:f}".rstrip("0").rstrip("."),
        "type": order_type,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
    }
    if limit_price is not None:
        payload["limit_price"] = f"{limit_price:.2f}"

    # Logged BEFORE the call: if the process dies mid-request, the log is the only evidence the
    # attempt happened. Never logs credentials — the client redacts, and none are in the payload.
    logger.warning(
        "SUBMITTING ORDER %s %s %s qty=%s type=%s limit=%s tif=%s env=%s client_order_id=%s",
        side.upper(), symbol, "→", payload["qty"], order_type,
        payload.get("limit_price", "—"), time_in_force,
        "paper" if client.is_paper else "LIVE", client_order_id,
    )

    import requests

    url = f"{client.base_url}{ORDERS_PATH}"
    headers = {
        "APCA-API-KEY-ID": client._key_id,
        "APCA-API-SECRET-KEY": client._secret,
        "content-type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=client._timeout_s)
    except Exception as exc:  # noqa: BLE001 — every network failure here is ambiguous by definition
        # DO NOT retry inside this function. A retry decision needs the audit row written first, and
        # only the caller knows whether one was.
        raise SubmissionUncertain(
            f"the order request did not complete: {client._redact(str(exc))}. Whether Alpaca "
            f"received it is unknown — reconcile by client_order_id {client_order_id!r} before "
            f"resubmitting."
        ) from None

    if resp.status_code in (401, 403):
        raise ExecutionRefused(f"Alpaca refused the order with {resp.status_code} (credentials)")
    if resp.status_code == 422:
        # Alpaca's validation: insufficient buying power, symbol not tradable, market closed. A
        # refusal with a reason, not an outage — surfaced verbatim because the operator can act on it.
        detail = resp.text[:400]
        raise ExecutionRefused(f"Alpaca rejected the order: {detail}")
    if resp.status_code >= 500:
        # A 5xx after the request arrived may or may not have created an order.
        raise SubmissionUncertain(
            f"Alpaca returned {resp.status_code}; whether the order was created is unknown — "
            f"reconcile by client_order_id {client_order_id!r} before resubmitting."
        )
    if not resp.ok:
        raise AlpacaError(f"Alpaca order submission returned {resp.status_code}")

    try:
        order = resp.json()
    except ValueError:
        raise SubmissionUncertain(
            f"Alpaca returned an undecodable response to the submission; reconcile by "
            f"client_order_id {client_order_id!r} before resubmitting."
        ) from None

    logger.warning(
        "ORDER ACCEPTED %s id=%s status=%s client_order_id=%s",
        symbol, order.get("id"), order.get("status"), client_order_id,
    )
    return order


def fetch_order_by_client_id(client: AlpacaClient, client_order_id: str) -> dict | None:
    """Look an order up by OUR id — the resolution for an uncertain submission.

    This is what turns "nobody knows" into a fact, and it is why client_order_id is generated
    upstream and stored before submission: without it there is no way to ask "did the thing I tried
    to do happen?" except by guessing from a list of recent orders.

    Returns the order, or None **only when Alpaca positively answered that no such order exists**
    (404, verified against the live API: `{"code":40410000,"message":"order not found for ..."}`).

    ANY OTHER FAILURE RAISES. The first draft of this function caught every error and returned None,
    which collapsed "this order does not exist" and "I could not find out" into the same value — and
    the caller acting on that during reconciliation would resubmit an order that had in fact landed.
    A lookup that cannot distinguish those two answers is worse than no lookup, because it converts
    "unknown" into a confident wrong answer, which is the exact bug client_order_id exists to stop.
    """
    import requests

    url = f"{client.base_url}{ORDERS_PATH}:by_client_order_id"
    headers = {
        "APCA-API-KEY-ID": client._key_id,
        "APCA-API-SECRET-KEY": client._secret,
        "accept": "application/json",
    }
    try:
        resp = requests.get(
            url, headers=headers, params={"client_order_id": client_order_id},
            timeout=client._timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        raise SubmissionUncertain(
            f"could not reach Alpaca to resolve {client_order_id!r}: "
            f"{client._redact(str(exc))}. Do NOT resubmit on this — the order may exist."
        ) from None

    if resp.status_code == 404:
        return None  # a positive answer: no such order
    if not resp.ok:
        raise SubmissionUncertain(
            f"Alpaca returned {resp.status_code} resolving {client_order_id!r}; whether the order "
            f"exists is still unknown. Do NOT resubmit on this."
        )
    try:
        return resp.json()
    except ValueError:
        raise SubmissionUncertain(
            f"Alpaca returned an undecodable response resolving {client_order_id!r}."
        ) from None
