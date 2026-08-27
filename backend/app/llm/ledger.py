"""Reading and writing the per-key spend ledger (migration 028).

Every function here is best-effort with respect to the DATABASE and strict with respect to the
NUMBERS. A ledger write must never take down a debate — an unrecordable call is a gap in the
accounts, not a reason to stop working — but a number that reaches the table has to be right,
because this is the record two people settle up from.
"""

from __future__ import annotations

import logging

from app.db import connection
from app.llm.pricing import PRICING_VERSION, cost_usd

logger = logging.getLogger("agentic.llm.ledger")


def record(
    *,
    provider: str,
    key_owner: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    purpose: str | None = None,
) -> None:
    """Write one call's usage. Never raises.

    Swallowed by design: this runs inside the debate's hot path, and a database blip must not turn a
    working jury into a failed one. It is logged at WARNING rather than silently, because a ledger
    with holes is only trustworthy if the holes announced themselves.
    """
    cost = cost_usd(model, input_tokens, output_tokens)
    if cost is None:
        # Loud. An unpriced model means this owner's total is understated from here on, and the
        # fix is a line in pricing.py — not something to discover while settling up.
        logger.warning(
            "no published rate for model %r — usage recorded, cost left NULL (add it to "
            "app/llm/pricing.py and bump PRICING_VERSION)",
            model,
        )
    try:
        with connection() as conn:
            conn.execute(
                "INSERT INTO llm_usage (provider, key_owner, model, purpose, calls,"
                " input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd,"
                " pricing_version) VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s,%s)",
                (
                    provider, key_owner, model, purpose,
                    max(0, int(input_tokens)), max(0, int(output_tokens)),
                    max(0, int(cache_read_tokens)), cost, PRICING_VERSION,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — a gap in the accounts, not a reason to stop working
        logger.warning("could not record LLM usage (%s %s): %s", provider, key_owner, exc)


def _balance_metric(provider: str | None) -> str:
    """Whether to balance this provider on dollars or on tokens.

    Dollars are the thing being split, so they win wherever they exist. But Gemini has no published
    rate in pricing.py, so every Gemini row carries a NULL cost — and balancing on dollars there
    would see 0.00 for both owners forever and keep picking slot 1, which is Joe. One owner would
    silently carry the entire Gemini bill.

    Tokens are always recorded, and WITHIN one provider running one model they are exactly
    proportional to cost. So the fallback is not an approximation of the right answer, it IS the
    right answer for the case it covers — it only degrades if a provider runs several models at
    different rates, which the jury does not.
    """
    return "tokens" if provider == "gemini" else "cost"


def spend_by_owner(provider: str | None = None) -> dict[str, float]:
    """Estimated USD per owner, for the selection path.

    An EMPTY dict on failure, not a raised error — and that has a consequence worth stating: with no
    spend history, selection falls back to slot order, which means a database outage sends every
    call to the same owner's key. That is a fair-share bug, not a correctness one, and it is
    strictly better than refusing to run a debate because the accounting is unreachable.
    """
    try:
        with connection() as conn:
            column = (
                "input_tokens + output_tokens"
                if _balance_metric(provider) == "tokens"
                else "estimated_cost_usd"
            )
            rows = conn.execute(
                f"SELECT key_owner, {column} FROM llm_spend_by_owner"
                " WHERE (%s::text IS NULL OR provider = %s)",
                (provider, provider),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — see the docstring: empty means slot-order fallback
        logger.warning("could not read spend totals; falling back to slot order: %s", exc)
        return {}
    return {r[0]: float(r[1] or 0) for r in rows}


def totals() -> dict:
    """The full split, for the API. Reports what is NOT priced alongside what is."""
    try:
        with connection() as conn:
            rows = conn.execute(
                "SELECT provider, key_owner, calls, input_tokens, output_tokens,"
                " estimated_cost_usd, unpriced_rows FROM llm_spend_by_owner"
                " ORDER BY provider, key_owner"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — the page degrades; it does not 500
        logger.warning("could not read the spend ledger: %s", exc)
        # Same SHAPE as the healthy path, not a smaller one. A response whose keys depend on
        # whether the database was reachable forces every consumer to handle two shapes, and the
        # one that forgets breaks only during an outage — the worst time to find out.
        return {
            "owners": [],
            "total_usd": 0.0,
            "pricing_version": PRICING_VERSION,
            "spread_usd_by_provider": {},
            "note": "the spend ledger is unavailable; these totals are not a measurement",
        }

    # Every CONFIGURED owner, including those with no rows yet. The view only knows owners who have
    # spent something, so computing the spread from it alone gave $0.00 when one owner was at $9
    # and the other had never been used — the exact imbalance this whole feature exists to close,
    # reported as already even. Absent is not zero for reporting, even though it is for selection.
    from app.llm import keys as key_registry

    configured = {(k.provider, k.owner) for k in key_registry.available()}
    seen = {(r[0], r[1]) for r in rows}
    zero_rows = [
        (provider, owner, 0, 0, 0, 0, 0)
        for provider, owner in sorted(configured - seen)
    ]

    owners = [
        {
            "provider": r[0],
            "owner": r[1],
            "calls": int(r[2] or 0),
            "input_tokens": int(r[3] or 0),
            "output_tokens": int(r[4] or 0),
            "estimated_cost_usd": float(r[5] or 0),
            # Rows whose model had no published rate. Their tokens are counted; their dollars are
            # not. Surfaced so a total is never read as complete when it is not.
            "unpriced_rows": int(r[6] or 0),
        }
        for r in [*rows, *zero_rows]
    ]
    owners.sort(key=lambda o: (o["provider"], o["owner"]))
    total = sum(o["estimated_cost_usd"] for o in owners)
    return {
        "owners": owners,
        "total_usd": round(total, 6),
        "pricing_version": PRICING_VERSION,
        # The gap between the highest and lowest spender: how far from an even split we are, and the
        # number the selection policy is actively working to shrink.
        # Per provider, because an Anthropic dollar and a Gemini dollar are different bills. A
        # global spread would net one provider's imbalance against the other's and report a fair
        # split that neither owner would recognise.
        "spread_usd_by_provider": {
            provider: round(
                max(o["estimated_cost_usd"] for o in owners if o["provider"] == provider)
                - min(o["estimated_cost_usd"] for o in owners if o["provider"] == provider),
                6,
            )
            for provider in sorted({o["provider"] for o in owners})
        },
        "note": (
            "Costs are estimated from a documented price list, not from a billing API. "
            "Reconcile against the real invoice before settling up."
        ),
    }
