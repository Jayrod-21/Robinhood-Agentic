#!/usr/bin/env python3
"""Write the account snapshot file from Alpaca. The replacement for the Robinhood MCP refresh.

WHAT THIS IS FOR, PRECISELY
    When Alpaca credentials are set, services/broker.py reads the broker LIVE on every request
    (5-second cache) and never opens this file. So this job does not make the dashboard fresher —
    the dashboard is already seconds old.

    What it does is keep the FALLBACK honest. The file is what the app serves if Alpaca becomes
    unreachable or the credentials are pulled, and until now it was a Robinhood export that only a
    manual button could refresh — through a host daemon that shells out to wt.exe, which does not
    exist on this machine. So the fallback was months stale and had no working way to update.

    Run every minute, the fallback is never worse than a minute behind the broker. That is the whole
    claim; it is deliberately smaller than "refresh the dashboard", which needs no help.

WHY IT DOES NOT TOUCH THE DATABASE
    The position mirror is the marking job's input and is synced by bin/nightly_marks.sh before the
    book is valued. Re-mirroring every minute would write to a table nothing reads between marks,
    and would put a container spawn on the minute-by-minute path for no gain. One job, one claim.

EXIT CODES
    0 written · 1 broker or credentials unavailable (the previous file is left untouched)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.alpaca import AlpacaClient, AlpacaError, snapshot_from_alpaca

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alpaca_snapshot")


def main() -> int:
    target = Path(
        os.environ.get("AGENTIC_SNAPSHOT_PATH")
        or Path(__file__).resolve().parents[1] / "data" / "account_snapshot.json"
    )
    try:
        client = AlpacaClient()
        client.assert_paper()  # a live account must never be written here by a background job
        snapshot = snapshot_from_alpaca(
            client.account(), client.positions(), generated_at=datetime.now(timezone.utc)
        )
    except AlpacaError as exc:
        # The OLD file is deliberately left in place. A fallback that empties itself when the broker
        # is unreachable turns one outage into two: the live read fails AND the thing meant to cover
        # for it is now blank.
        logger.error("alpaca unavailable, leaving the existing snapshot untouched: %s", exc)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: the backend may read this file at any moment, and a half-written JSON document would
    # be served as "the account could not be read" — a broker outage invented by the writer.
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".snapshot-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True, default=str)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    logger.info(
        "wrote %s: %d position(s), cash %.2f, generated_at %s",
        target.name, len(snapshot.get("positions") or []),
        (snapshot.get("account") or {}).get("cash", 0.0), snapshot.get("generated_at"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
