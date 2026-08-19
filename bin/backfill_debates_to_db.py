#!/usr/bin/env python3
"""Load the debate records already on disk into the relational model.

Eight debates existed as logs/debates/*.json while `debates` and `judgments` held nothing, because
the engine only ever wrote files. New debates persist both ways now; this brings the existing ones
across so calibration has a history to work with rather than starting from today.

Idempotent by re-reading, not by guessing: a record whose ticker already has a debate at the same
started_at is skipped, so running this twice does not double-count a juror's track record.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backfill_debates")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=None, help="debates directory (default: the configured one)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    from app.config import get_settings
    from app.db import connection
    from app.services.debate_store import persist_debate

    directory = Path(args.dir) if args.dir else get_settings().debates_dir
    files = sorted(directory.glob("dbt-*.json"))
    if not files:
        logger.info("no debate records in %s", directory)
        return 0

    stored = skipped = failed = 0
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("%s: unreadable (%s)", path.name, exc)
            failed += 1
            continue

        ticker = (record.get("ticker") or "").upper()
        with connection() as conn:
            existing = conn.execute(
                "SELECT d.id FROM debates d JOIN securities s ON s.id = d.security_id"
                " WHERE upper(s.symbol) = %s AND d.started_at = %s",
                (ticker, record.get("created_at")),
            ).fetchone()
        if existing:
            logger.info("%s: already stored as debates.id=%s", path.name, existing[0])
            skipped += 1
            continue

        if not args.apply:
            logger.info("%s: would store (%s)", path.name, ticker)
            continue
        if persist_debate(record) is None:
            failed += 1
        else:
            stored += 1

    if not args.apply:
        logger.info("DRY RUN — %d file(s) inspected, %d already stored. Re-run with --apply.",
                    len(files), skipped)
        return 0
    logger.info("stored %d, skipped %d, failed %d", stored, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
