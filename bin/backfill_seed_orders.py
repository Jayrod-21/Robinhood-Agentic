#!/usr/bin/env python3
"""Backfill audit rows for orders that were PLACED but never recorded.

WHY THIS EXISTS
    bin/seed_paper_book.py gated its writes on `if dsn:` while proving reachability with
    psycopg.connect(dsn). In the container DATABASE_URL is unset and PG* carries the connection, so
    the check passed and every write was skipped: fourteen real orders, an empty audit table, and a
    success message.

    The orders are real and Alpaca has the authoritative record, so the trail is reconstructible.
    Reconstructed rows are marked as such in `preview` — an audit row written after the fact from
    the broker's copy is weaker evidence than one written before submission, and saying so is the
    difference between a repaired trail and a forged one.

IDEMPOTENT
    ON CONFLICT (client_order_id) DO NOTHING. Re-running adds nothing.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from src.alpaca import AlpacaClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backfill_seed_orders")

PREFIX = "seed-"


def main() -> int:
    client = AlpacaClient()
    client.assert_paper()
    orders = client.get("/v2/orders", {"status": "all", "limit": 500})
    seeded = [o for o in orders if str(o.get("client_order_id", "")).startswith(PREFIX)]
    if not seeded:
        logger.info("no seed orders at the broker; nothing to backfill")
        return 0

    acct = client.account()
    masked = "••••" + str(acct.get("account_number", ""))[-4:]
    inserted = 0
    with psycopg.connect(__import__("os").environ.get("DATABASE_URL", "")) as conn:
        for o in seeded:
            coid = o["client_order_id"]
            preview = {
                "seed": True,
                "reconstructed": True,
                "note": (
                    "Backfilled from the broker AFTER submission: the seeding script skipped its "
                    "audit writes. Weaker evidence than a row written before the order was sent."
                ),
                "client_order_id": coid,
            }
            row = conn.execute(
                "INSERT INTO orders (client_order_id, preview_id, preview, broker_env,"
                " account_masked, symbol, side, order_type, time_in_force, requested_notional,"
                " guardrails_passed, submit_status, broker_order_id, broker_status, filled_qty,"
                " filled_avg_price, reconciled_at)"
                " VALUES (%s,%s,%s,'alpaca-paper',%s,%s,%s,%s,%s,%s,true,'accepted',%s,%s,%s,%s,now())"
                " ON CONFLICT (client_order_id) DO NOTHING RETURNING id",
                (
                    coid, coid.rsplit("-", 1)[0], json.dumps(preview), masked,
                    o.get("symbol"), o.get("side"), o.get("order_type") or o.get("type"),
                    o.get("time_in_force"),
                    float(o["notional"]) if o.get("notional") else None,
                    str(o.get("id"))[:128], o.get("status"),
                    float(o["filled_qty"]) if o.get("filled_qty") else None,
                    float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
                ),
            ).fetchone()
            if row:
                inserted += 1
    logger.info("backfilled %d of %d seed orders", inserted, len(seeded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
