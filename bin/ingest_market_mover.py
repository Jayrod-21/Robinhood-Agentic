#!/usr/bin/env python3
"""Turn exported Market Mover emails into the brief the dashboard reads.

WHAT IT WRITES
    <data_dir>/market_mover/latest.json     the newest brief — what /api/market-context serves
    <data_dir>/market_mover/archive/*.json  one per brief, kept as history

WHY latest.json IS CHOSEN BY SENT TIME, NOT BY FILENAME
    The export names files 402.eml, 820.eml, 821.eml — which sorts lexically, not chronologically,
    and would put 99.eml after 820.eml. The Date header is the only thing that actually says when a
    brief was published.

A BRIEF THAT WILL NOT PARSE IS AN ERROR, NEVER AN EMPTY DAY
    routers/market_context.py draws a hard line between "nothing published" and "a brief exists we
    could not read", because only one of those is a reason to go looking. So a file that fails to
    parse is reported and counted, and if NOTHING parses this refuses to write at all rather than
    replacing a good latest.json with an empty one.

THE CONTENT IS UNTRUSTED
    Third-party editorial text, rendered to an operator and read by the assistant. Stored and served
    as DATA — never interpolated into a prompt, never treated as instructions.

Exit codes: 0 ok · 1 nothing parsed · 2 write failure
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.market_mover import MarketMoverParseError, parse_email

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest_market_mover")

EXIT_OK, EXIT_NOTHING, EXIT_WRITE = 0, 1, 2


def _write_atomic(path: Path, payload: dict) -> None:
    """Write JSON atomically — the backend may read this file at any moment, and a half-written
    document would be served as 'the brief is unreadable', i.e. an outage this script invented."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mm-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="a .eml file, or a directory of them")
    ap.add_argument("--data-dir", default=None,
                    help="where market_mover/ lives (default: the app's configured data dir)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.source).expanduser()
    files = sorted(src.glob("*.eml")) if src.is_dir() else [src]
    if not files:
        logger.error("no .eml files under %s", src)
        return EXIT_NOTHING

    briefs, failures = [], []
    for f in files:
        try:
            briefs.append(parse_email(f))
        except MarketMoverParseError as exc:
            # Named individually. A count alone cannot be acted on — the point of reporting a
            # failure is being able to open the one that broke.
            failures.append((f.name, str(exc)))
        except Exception as exc:  # noqa: BLE001 — one bad mail must not abort the batch
            failures.append((f.name, f"{type(exc).__name__}: {exc}"))

    for name, why in failures:
        logger.warning("could not parse %s: %s", name, why)

    if not briefs:
        logger.error(
            "%d file(s) inspected and NONE parsed. Refusing to write: replacing a good brief with "
            "an empty one would read on the dashboard as a quiet news day.", len(files),
        )
        return EXIT_NOTHING

    # By sent time. The export's filenames sort lexically, which is not chronological.
    briefs.sort(key=lambda b: b.get("generated_at") or "")
    newest = briefs[-1]

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else _default_data_dir()
    root = data_dir / "market_mover"

    total_headlines = sum(len(b["headlines"]) for b in briefs)
    logger.info(
        "parsed %d brief(s), %d headline(s); %d unreadable. Newest: %s (%s)",
        len(briefs), total_headlines, len(failures),
        newest.get("brief_date") or newest.get("generated_at"), newest.get("subject", "")[:60],
    )

    if args.dry_run:
        logger.info("DRY RUN — nothing written. Would write %s and %d archive file(s).",
                    root / "latest.json", len(briefs))
        return EXIT_OK

    try:
        _write_atomic(root / "latest.json", newest)
        for brief in briefs:
            stamp = brief.get("brief_date") or (brief.get("generated_at") or "unknown")[:10]
            _write_atomic(root / "archive" / f"{stamp}.json", brief)
    except OSError as exc:
        logger.error("write failed: %s", exc)
        return EXIT_WRITE

    logger.info("wrote %s and %d archived brief(s) to %s",
                root / "latest.json", len(briefs), root / "archive")
    return EXIT_OK


def _default_data_dir() -> Path:
    """Where to write, resolved for the HOST rather than for the container.

    app.config's data_dir is /app/data, which is correct inside the backend container and does not
    exist out here — this script runs on the host. deploy/docker-compose.prod.yml mounts the repo's
    ./data there, so writing to the repo directory puts the file exactly where the container reads
    it. Asking the app produced a confident, unusable path.
    """
    return Path(__file__).resolve().parents[1] / "data"


if __name__ == "__main__":
    sys.exit(main())
