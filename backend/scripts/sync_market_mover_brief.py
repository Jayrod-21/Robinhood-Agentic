#!/usr/bin/env python3
"""Fetch the Market Mover brief and write it where the market-context route reads it.

Market Mover (a separate project) publishes its latest brief as JSON on GitHub Pages. The
market-context route (backend/app/routers/market_context.py) reads that brief from a LOCAL file at
``$DATA_DIR/market_mover/latest.json`` and deliberately makes no outbound calls of its own (ADR-001:
rh-db has no network port; the route only reads a file the backend already has). This script is the
bridge between the two: a small, dependency-free job that pulls the published URL and writes the
local file, meant to run on a timer on the backend host (systemd timer or cron).

Design notes:
  * Stdlib only (urllib), so it runs in any Python 3 without the backend venv.
  * The fetched JSON is THIRD-PARTY TEXT. It is written to disk as data and never executed or
    interpolated; the route and the frontend already treat every field as untrusted. This script
    only validates that it parses as a JSON object before writing.
  * A bad fetch (network error, non-200, non-JSON, or a payload that isn't an object) exits non-zero
    and leaves any existing good file UNTOUCHED, so a transient upstream hiccup never blanks the
    Market page.
  * The write is atomic (temp file + os.replace) so a reader never sees a half-written file.

Usage:
    python3 backend/scripts/sync_market_mover_brief.py
    # overrides:
    MARKET_MOVER_BRIEF_URL=https://…/latest.json DATA_DIR=/app/data \\
        python3 backend/scripts/sync_market_mover_brief.py
    python3 backend/scripts/sync_market_mover_brief.py --url … --data-dir /app/data

Wire it (example, every 30 min via cron on the backend host):
    */30 * * * * cd /path/to/Robinhood-Agentic && python3 backend/scripts/sync_market_mover_brief.py >> /var/log/mm-brief-sync.log 2>&1

Exit codes: 0 = wrote (or content unchanged), 1 = fetch/parse failed (existing file kept),
2 = write failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://joewhitejr.github.io/Market_News/latest.json"
DEFAULT_DATA_DIR = "/app/data"
FETCH_TIMEOUT_SECS = 15
MAX_BYTES = 5_000_000  # a brief is a few KB; cap the read so a wrong URL can't stream forever.


def _log(msg: str) -> None:
    print(f"[sync_market_mover_brief] {msg}", file=sys.stderr)


def fetch_brief(url: str) -> dict:
    """GET the brief and return it as a dict. Raises on any network/HTTP/JSON/shape problem."""
    req = urllib.request.Request(url, headers={"User-Agent": "ww-market-mover-brief-sync/1"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECS) as resp:  # noqa: S310 (fixed https URL)
        # Real HTTP handlers set .status (200 on success); some handlers (e.g. file://) leave it
        # None. Treat only an explicit non-200 as a failure so a genuine 404/500 is caught.
        status = getattr(resp, "status", None)
        if status is not None and status != 200:
            raise RuntimeError(f"unexpected HTTP status {status}")
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise RuntimeError(f"brief exceeds {MAX_BYTES} bytes; refusing (wrong URL?)")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("brief is not a JSON object")
    return data


def write_atomic(target: Path, data: dict) -> None:
    """Write ``data`` as pretty JSON to ``target`` atomically (temp file in the same dir + replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".latest.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the Market Mover brief to the backend's local file.")
    parser.add_argument("--url", default=os.environ.get("MARKET_MOVER_BRIEF_URL", DEFAULT_URL))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    args = parser.parse_args(argv)

    target = Path(args.data_dir) / "market_mover" / "latest.json"

    try:
        brief = fetch_brief(args.url)
    except Exception as exc:  # noqa: BLE001 (any failure here means "keep the file we have")
        _log(f"fetch failed ({exc!r}); leaving existing {target} untouched")
        return 1

    # If the content is byte-identical to what's on disk, skip the rewrite (quieter, no mtime churn).
    new_text = json.dumps(brief, indent=2, ensure_ascii=False)
    if target.exists():
        try:
            if target.read_text(encoding="utf-8") == new_text:
                _log(f"brief unchanged ({target}); nothing to do")
                return 0
        except OSError:
            pass  # unreadable current file, go ahead and overwrite it below

    try:
        write_atomic(target, brief)
    except Exception as exc:  # noqa: BLE001
        _log(f"write failed ({exc!r})")
        return 2

    n_headlines = len(brief.get("headlines") or [])
    n_movers = len(brief.get("top_movers") or [])
    _log(f"wrote {target}: brief_date={brief.get('brief_date')}, {n_headlines} headlines, {n_movers} movers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
