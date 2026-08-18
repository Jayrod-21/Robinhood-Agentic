#!/usr/bin/env python3
"""Seed the Alpaca PAPER book with an equal-dollar basket, and record every order.

WHY THIS IS A SCRIPT AND NOT THE AGENT
    The performance page reads portfolio_returns_daily, which is empty because nothing has ever
    traded. This puts real positions in the paper account so the marking job has something to value
    and the page has something to show. It is an OWNER action — a deliberate one-off — not the
    agentic loop deciding anything, and it is named that way so nobody later mistakes these fills
    for the strategy's track record.

WHAT IT DOES NOT DO
    It does not evaluate guardrails. Those exist to gate the AGENT's proposals against the charter;
    an owner seeding a paper book with an equal-weight basket is not that, and running the
    exit-before-entry rule here would demand a written thesis for fourteen names chosen to generate
    data. That is a real gap and it is stated rather than hidden: these orders bypass §5.

    It refuses outright against a live endpoint. assert_paper() is not optional here.

EVERY ORDER IS STILL AUDITED
    Rows land in `orders` before submission with submit_status='submitting', exactly as the real
    path does, so a fill that vanishes mid-request still leaves evidence. client_order_id is
    deterministic per run, so a re-run collides instead of double-buying.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psycopg

from src.alpaca import AlpacaClient, snapshot_from_alpaca
from src.alpaca_execution import (
    ExecutionRefused,
    SubmissionUncertain,
    submit_order,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed_paper_book")

BASKET = [
    "AMD", "NVDA", "GM", "MSFT", "QBTS", "ISRG", "GLD",
    "BRK.B", "BE", "QCOM", "VST", "V", "CVX", "SVRA",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seed_paper_book")
    ap.add_argument("--notional", type=float, default=500.0, help="dollars per name")
    ap.add_argument("--tag", required=True, help="run tag; makes client_order_id deterministic")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-audit", action="store_true",
                    help="place without recording rows (requires saying so out loud)")
    args = ap.parse_args(argv)

    client = AlpacaClient()
    client.assert_paper()  # never a live account, whatever the environment says

    acct = client.account()
    equity = float(acct.get("equity") or 0)
    needed = args.notional * len(BASKET)
    logger.info("account equity $%.2f | deploying $%.2f across %d names", equity, needed, len(BASKET))
    if needed > equity:
        logger.error("basket needs $%.2f but the account holds $%.2f", needed, equity)
        return 1

    # Empty DSN means "use the libpq PG* environment", which is how every db job here connects —
    # rh-db has no host port (ADR-001), so this runs in a container on rh-internal.
    #
    # AUDIT IS A FLAG, NOT THE TRUTHINESS OF THE DSN STRING. The first version of this script
    # gated every write on `if dsn:` while proving reachability with `psycopg.connect(dsn)`. Run in
    # the container, DATABASE_URL is unset and PG* carries the connection: the CHECK passed and
    # every WRITE was skipped. It placed fourteen real orders and reported success with an empty
    # audit table — after being written to refuse exactly that. Two guards, two different questions,
    # silently disagreeing.
    dsn = __import__("os").environ.get("DATABASE_URL", "")
    if not args.dry_run and not args.no_audit:
        try:
            psycopg.connect(dsn, connect_timeout=5).close()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cannot reach the database to record orders: %s\n"
                "Placing trades with no audit trail is a deliberate choice, not a fallback — "
                "re-run with --no-audit if that is genuinely what you want.", str(exc)[:160],
            )
            return 2
    audit = not args.no_audit and not args.dry_run
    if args.no_audit:
        logger.warning("--no-audit: orders will be PLACED with no row in `orders`")
    placed, failed = [], []

    for symbol in BASKET:
        coid = f"seed-{args.tag}-{symbol.replace('.', '')}"
        if args.dry_run:
            logger.info("DRY RUN %s $%.2f (client_order_id=%s)", symbol, args.notional, coid)
            continue

        row_id = None
        if audit:
            with psycopg.connect(dsn) as conn:
                row_id = conn.execute(
                    "INSERT INTO orders (client_order_id, preview_id, preview, broker_env,"
                    " account_masked, symbol, side, order_type, time_in_force,"
                    " requested_notional, guardrails_passed, submit_status)"
                    " VALUES (%s,%s,%s,%s,%s,%s,'buy','market','day',%s,true,'submitting')"
                    " ON CONFLICT (client_order_id) DO NOTHING RETURNING id",
                    (coid, f"seed-{args.tag}",
                     json.dumps({"seed": True, "tag": args.tag, "notional": args.notional,
                                 "note": "owner seeding, guardrails bypassed by design"}),
                     "alpaca-paper", "••••" + str(acct.get("account_number", ""))[-4:],
                     symbol, args.notional),
                ).fetchone()
            if row_id is None:
                logger.warning("%s already seeded under tag %s — skipping", symbol, args.tag)
                continue
            row_id = row_id[0]

        try:
            order = submit_order(
                client=client, client_order_id=coid, symbol=symbol, side="buy",
                order_type="market", notional=args.notional,
                allowed_types=["market"], allow_live=False,
            )
        except (ExecutionRefused, SubmissionUncertain) as exc:
            failed.append((symbol, str(exc)[:120]))
            if audit and row_id:
                status = "unknown" if isinstance(exc, SubmissionUncertain) else "rejected"
                with psycopg.connect(dsn) as conn:
                    conn.execute(
                        "UPDATE orders SET submit_status=%s, submit_error=%s, updated_at=now()"
                        " WHERE id=%s", (status, str(exc)[:500], row_id),
                    )
            continue

        placed.append((symbol, order.get("id"), order.get("status")))
        if audit and row_id:
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    "UPDATE orders SET submit_status='accepted', broker_order_id=%s,"
                    " broker_status=%s, updated_at=now() WHERE id=%s",
                    (str(order.get("id"))[:128], order.get("status"), row_id),
                )

    logger.info("placed %d, failed %d", len(placed), len(failed))
    for s, oid, st in placed:
        logger.info("  %-6s %s %s", s, st, oid)
    for s, err in failed:
        logger.error("  %-6s FAILED %s", s, err)

    snap = snapshot_from_alpaca(client.account(), client.positions(),
                                generated_at=datetime.now(timezone.utc))
    logger.info("account now: $%.2f total, %d positions, $%.2f cash",
                snap["account"]["total_value"], len(snap["positions"]), snap["account"]["cash"])
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
