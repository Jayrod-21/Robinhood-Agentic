#!/usr/bin/env python3
"""One-off repair: drop annual observations dated by the pre-fix naive-Eastern known_at.

WHAT WENT WRONG
    FMP returns `acceptedDate` as a naive timestamp in US/Eastern ("2025-10-31 06:01:26"). It was
    stored as if it were already UTC, which dated every filing four or five hours EARLIER than it
    happened. On a point-in-time table that is the one direction that matters: a backtest filtering
    on known_at would have been handed a filing before the market had it.

    src/data.py now converts Eastern -> UTC explicitly, and a re-ingest wrote correct rows. Because
    the observation key is (security_id, period_end, period_type, known_at), the corrected rows did
    not replace the old ones — they landed BESIDE them, so each affected period shows twice.

WHAT THIS DELETES, AND WHAT IT REFUSES TO
    Only a row that has a twin with:
      * the same security, period_end and period_type,
      * a later created_at (so the corrected reload actually happened),
      * a known_at exactly 4 or 5 hours later (the Eastern offset — not an arbitrary delta),
      * identical revenue, EBITDA and EPS.

    That last condition is what keeps a genuine RESTATEMENT safe. A restatement changes the numbers
    and arrives with its own filing date; the point of keeping observations keyed by known_at is to
    preserve exactly that. This script removes clock-shifted duplicates of the same figures and
    nothing else.

    Default is a dry run. Pass --apply to delete.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("repair_prefix_known_at")

# Selects the STALE row (old) of each pair. Conditions are ANDed deliberately: any one of them alone
# would be too broad to run against a table whose whole purpose is keeping every observation.
DOOMED = """
SELECT old.id, s.symbol, old.period_end, old.known_at AS stale_known_at, new.known_at AS correct_known_at
FROM fundamentals_snapshots old
JOIN fundamentals_snapshots new
  ON  new.security_id = old.security_id
  AND new.period_end  = old.period_end
  AND new.period_type = old.period_type
  AND new.created_at  > old.created_at
JOIN securities s ON s.id = old.security_id
WHERE old.period_type = 'annual'
  AND new.known_at - old.known_at IN (INTERVAL '4 hours', INTERVAL '5 hours')
  AND new.revenue_ttm IS NOT DISTINCT FROM old.revenue_ttm
  AND new.ebitda_ttm  IS NOT DISTINCT FROM old.ebitda_ttm
  AND new.eps_current IS NOT DISTINCT FROM old.eps_current
ORDER BY s.symbol, old.period_end DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="delete the rows (default: dry run)")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    with psycopg.connect(dsn) if dsn else psycopg.connect() as conn:
        rows = conn.execute(DOOMED).fetchall()
        if not rows:
            logger.info("nothing to repair — no clock-shifted duplicate observations found")
            return 0
        for _id, symbol, period_end, stale, correct in rows:
            logger.info("%-6s %s  %s -> %s", symbol, period_end, stale, correct)
        logger.info("%d duplicate observation(s) identified", len(rows))

        if not args.apply:
            logger.info("DRY RUN — nothing deleted. Re-run with --apply to remove these.")
            return 0

        ids = [r[0] for r in rows]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fundamentals_snapshots WHERE id = ANY(%s)", (ids,))
            deleted = cur.rowcount
        conn.commit()
        logger.info("deleted %d row(s)", deleted)

        remaining = conn.execute(
            "SELECT count(*), count(DISTINCT (security_id, period_end))"
            " FROM fundamentals_snapshots WHERE period_type='annual'"
        ).fetchone()
        logger.info("annual rows now: %d across %d distinct periods", remaining[0], remaining[1])
        if remaining[0] != remaining[1]:
            logger.warning(
                "%d annual row(s) still share a period with another. That is LEGITIMATE for a "
                "restatement — check them before assuming this script under-ran.",
                remaining[0] - remaining[1],
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
