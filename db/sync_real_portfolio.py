#!/usr/bin/env python3
"""Mirror the broker's holdings into a `kind='real'` paper_portfolio, so the marking job can value it.

WHY A PORTFOLIO ROW FOR A REAL ACCOUNT
    `portfolio_returns_daily` is keyed on a portfolio, and the marking job values portfolios. The
    real account is one — `paper_portfolios.kind` already allows 'real', which the schema author
    anticipated. Without this row the account has no equity curve, and the performance page has
    nothing to draw.

WHAT IT IS NOT
    Not a ledger of trades. It is a MIRROR of what the broker says is held right now, refreshed by
    re-running. The `orders` table is the trade record; this is the position snapshot the valuation
    reads. Keeping them separate matters: a mirror that drifted from the broker would be a second
    opinion about your own holdings, and there is no version of that which is useful.

    So each sync REPLACES the open positions rather than appending. Re-running after a fill updates
    the mirror; it does not create a duplicate lot.

UNPRICEABLE OR UNKNOWN SYMBOLS ARE REPORTED, NOT DROPPED
    A holding whose security is missing from `securities` cannot be valued, and silently omitting it
    would understate the book — an equity curve quietly missing a position is worse than no curve,
    because it looks complete.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from src.alpaca import AlpacaClient, snapshot_from_alpaca

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sync_real_portfolio")

EXIT_OK, EXIT_FAIL = 0, 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sync_real_portfolio")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    client = AlpacaClient()
    snap = snapshot_from_alpaca(client.account(), client.positions(),
                               generated_at=datetime.now(timezone.utc))
    positions = snap["positions"]
    account = snap["account"]
    if not positions:
        logger.warning("the broker reports no positions; nothing to mirror")

    dsn = __import__("os").environ.get("DATABASE_URL", "")
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT id, inception_date FROM paper_portfolios "
            "WHERE kind = 'real' AND closed_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            # Every portfolio belongs to an agent (paper_portfolios.agent_id is NOT NULL), and the
            # schema anticipated this case: agents.kind allows 'real'. The owner is the actor for a
            # book they trade themselves — inventing a fake persona to satisfy the FK would put a
            # decision-maker in the record that never decided anything.
            agent = conn.execute(
                "SELECT id FROM agents WHERE agent_key = 'owner' AND kind = 'real'"
            ).fetchone()
            if agent is None:
                agent = conn.execute(
                    "INSERT INTO agents (agent_key, version, kind, display_name, notes)"
                    " VALUES ('owner', 1, 'real', 'Owner', %s) RETURNING id",
                    (
                        (
                            "The account holder. Attributed to positions an owner placed "
                            "directly, so they are never mistaken for an agent's track record."
                        ),
                    ),
                ).fetchone()
                logger.info("created the 'owner' agent: id=%s", agent[0])
            agent_id = agent[0]
            # base_value is what the book started at, which anchors cumulative return. Using today's
            # equity would declare the account flat at inception whatever it actually did.
            base = float(account["total_value"])
            row = conn.execute(
                "INSERT INTO paper_portfolios (kind, agent_id, strategy_mode, inception_date,"
                " base_value, cash) VALUES ('real', %s, 'buy_and_hold', current_date, %s, %s)"
                " RETURNING id, inception_date",
                (agent_id, base, float(account["cash"])),
            ).fetchone()
            logger.info("created the real portfolio: id=%s base=$%.2f", row[0], base)
        portfolio_id, inception = row[0], row[1]

        missing: list[str] = []
        rows = []
        for p in positions:
            sec = conn.execute(
                "SELECT id FROM securities WHERE upper(symbol) = upper(%s)", (p["symbol"],)
            ).fetchone()
            if sec is None:
                missing.append(p["symbol"])
                continue
            rows.append((portfolio_id, sec[0], p["quantity"], p["average_buy_price"]))

        if missing:
            # Loud, and it fails the run: a curve quietly missing a position looks complete.
            logger.error(
                "%d held symbol(s) are not in `securities` and cannot be valued: %s. "
                "Load reference data for them before trusting any equity curve.",
                len(missing), ", ".join(sorted(missing)),
            )

        if args.dry_run:
            logger.info("DRY RUN — would mirror %d position(s) into portfolio %s",
                        len(rows), portfolio_id)
            return EXIT_FAIL if missing else EXIT_OK

        # Replace, never append: this is a mirror of what is held now, not a trade log.
        conn.execute(
            "DELETE FROM paper_portfolio_positions WHERE portfolio_id = %s AND exit_date IS NULL",
            (portfolio_id,),
        )
        conn.cursor().executemany(
            "INSERT INTO paper_portfolio_positions (portfolio_id, security_id, entry_date, shares,"
            " entry_price) VALUES (%s, %s, current_date, %s, %s)",
            rows,
        )
        conn.execute(
            "UPDATE paper_portfolios SET cash = %s, updated_at = now() WHERE id = %s",
            (float(account["cash"]), portfolio_id),
        )

    logger.info("portfolio %s (inception %s): mirrored %d position(s), cash $%.2f",
                portfolio_id, inception, len(rows), account["cash"])
    return EXIT_FAIL if missing else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
