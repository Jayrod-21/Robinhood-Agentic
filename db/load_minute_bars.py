"""Bulk loader for Polygon.io flat-file minute aggregates.

Loads `data/market/minute_bars_5y/YYYY/MM/YYYY-MM-DD.csv.gz` into `price_bars_minute`.

SCALE drives every decision here. A measured day file holds ~1.6M rows across ~9,000-10,400 symbols
— the full US equity universe at minute resolution — and the archive is 1,256 files, so on the order
of 2 BILLION rows. Three consequences:

    * STREAM, never buffer. Rows go to the server through COPY from a generator. Machine M has
      30 GiB of RAM and runs a second production stack (9b Korean Master) alongside; a DataFrame of
      one file is survivable, of the archive is not. Nothing here ever holds more than one row plus
      a symbol map.
    * PARTITIONS FIRST. `price_bars_minute` has no DEFAULT partition by design, so an insert with no
      matching partition raises rather than silently landing somewhere. Partitions are ensured for
      the file's ACTUAL timestamp span before any row is written — see the EST note below.
    * RESUMABLE. A load of this size will be interrupted. Each file's bytes are hashed and recorded
      in `data_sources`; a file whose hash is already present is skipped, so a re-run continues
      rather than duplicating.

THE MONTH-BOUNDARY TRAP — IT IS EVERY MONTH, NOT JUST WINTER
    Polygon day files carry post-market bars through 20:00 ET, and those cross UTC midnight in BOTH
    US timezone regimes. Verified against this archive:

        2020-11-30 (EST, UTC-5):  09:00 UTC -> 2020-12-01 00:59 UTC
        2021-06-30 (EDT, UTC-4):  08:00 UTC -> 2021-07-01 00:00 UTC

    In EST the 19:00-19:59 ET bars land after midnight; in EDT the 20:00 ET closing bar lands at
    exactly 00:00 UTC the next day. So a month-end file ALWAYS contains rows belonging to the
    following month — this was initially thought to be an EST-only hazard, and measuring it showed
    otherwise.

    Ensuring partitions from the filename's date therefore fails at the FIRST month-end, not the
    first winter one, and `price_bars_minute` has no DEFAULT partition to absorb the mistake. The
    partition call uses min(ts)..max(ts) read from the data, never the filename.

SYMBOL RESOLUTION
    `securities` allows one LIVE holder per symbol, not one ever: tickers are recycled, and a global
    unique would make a re-listed symbol overwrite the delisted company's identity. This loader only
    ever creates rows for symbols with no live holder, and resolves to the live row otherwise.
    Detecting a delisting is a separate concern (it needs absence over time, which a single file
    cannot show) and is deliberately not attempted here.

Usage:
    python db/load_minute_bars.py --root data/market/minute_bars_5y
    python db/load_minute_bars.py --root … --limit 5        # first 5 files, for a smoke test
    python db/load_minute_bars.py --root … --dry-run        # parse and report, write nothing
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - clear message beats a traceback
    print("load_minute_bars: psycopg (v3) is required. pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("load_minute_bars")

EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_SQL = 2
EXIT_CONNECTION = 3

PROVIDER = "polygon"
DATASET = "minute_bars"

# Mirrors ck_securities_symbol in migration 001. Applied here so a malformed ticker is counted and
# skipped with its reason, rather than aborting a 1.4M-row COPY partway through.
SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}(\.[A-Za-z0-9]{1,4}){0,2}$")

# Polygon writes window_start as a nanosecond epoch. Anything outside a sane window means the column
# meaning changed upstream (seconds? milliseconds?) and the load must stop rather than silently
# writing timestamps in 1970 or 2262.
NS_MIN = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
NS_MAX = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000_000)

EXPECTED_HEADER = ["ticker", "volume", "open", "close", "high", "low", "window_start", "transactions"]


class LoadError(Exception):
    """A failure this module raises deliberately."""


@dataclass
class FileStats:
    path: Path
    rows_read: int = 0
    rows_written: int = 0
    skipped_symbol: int = 0
    skipped_bad_row: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    min_ts: datetime | None = None
    max_ts: datetime | None = None
    seconds: float = 0.0

    def note_skip(self, reason: str) -> None:
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1


def sha256_of(path: Path) -> str:
    """Hash the file's bytes. Chunked — these are ~20 MB compressed and there are 1,256 of them."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise LoadError(f"data root not found: {root}")
    files = sorted(root.rglob("*.csv.gz"))
    if not files:
        raise LoadError(f"no .csv.gz files under {root}")
    return files


def _open_csv(path: Path) -> csv.reader:
    fh = gzip.open(path, "rt", newline="", encoding="utf-8")
    reader = csv.reader(fh)
    header = next(reader, None)
    if header != EXPECTED_HEADER:
        fh.close()
        raise LoadError(
            f"{path.name}: unexpected header {header!r}. Expected {EXPECTED_HEADER!r} — the "
            "provider's column set changed and the parser must be reviewed before loading."
        )
    return fh, reader


def scan_file(path: Path) -> tuple[set[str], datetime, datetime, int]:
    """First pass: distinct symbols and the true timestamp span.

    Two passes over a compressed file rather than one pass into memory. The symbol SET is small
    (~10k strings for the whole US universe); the rows are not, and holding 1.4M of them to avoid a
    second decompress would trade a bounded CPU cost for an unbounded memory one.
    """
    symbols: set[str] = set()
    lo = hi = None
    count = 0
    fh, reader = _open_csv(path)
    try:
        for row in reader:
            if len(row) != 8:
                continue
            count += 1
            symbols.add(row[0])
            try:
                ns = int(row[6])
            except ValueError:
                continue
            if not (NS_MIN <= ns <= NS_MAX):
                continue
            ts = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
            if lo is None or ts < lo:
                lo = ts
            if hi is None or ts > hi:
                hi = ts
    finally:
        fh.close()

    if lo is None or hi is None:
        raise LoadError(f"{path.name}: no rows with a usable window_start — refusing to load")
    return symbols, lo, hi, count


def resolve_symbols(conn: psycopg.Connection, symbols: set[str], source_id: int, seen_on: date) -> dict[str, int]:
    """Map symbol -> securities.id, creating rows for symbols with no live holder.

    Returns only symbols that resolved; malformed ones are left out and counted by the caller.
    """
    valid = sorted(s for s in symbols if SYMBOL_RE.match(s))
    if not valid:
        return {}

    with conn.cursor() as cur:
        # Insert first, then read back — one round trip each rather than per symbol. ON CONFLICT
        # targets the partial unique index (live rows only), so a delisted holder never blocks a
        # re-listing and a live holder is never duplicated.
        cur.executemany(
            "INSERT INTO securities (symbol, first_seen, source_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (symbol) WHERE delisted_at IS NULL DO NOTHING",
            [(s, seen_on, source_id) for s in valid],
        )
        cur.execute(
            "SELECT symbol, id FROM securities WHERE delisted_at IS NULL AND symbol = ANY(%s)",
            (valid,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def ensure_partitions(conn: psycopg.Connection, lo: datetime, hi: datetime) -> list[str]:
    """Create every monthly partition the file's ACTUAL span needs. See the EST note in the header."""
    with conn.cursor() as cur:
        cur.execute("SELECT ensure_price_bar_partitions(%s::date, %s::date)", (lo.date(), hi.date()))
        return cur.fetchone()[0]


def _rows(path: Path, symbol_ids: dict[str, int], source_id: int, stats: FileStats):
    """Second pass: yield COPY-ready tuples. Generator — one row in memory at a time."""
    fh, reader = _open_csv(path)
    try:
        for row in reader:
            stats.rows_read += 1
            if len(row) != 8:
                stats.skipped_bad_row += 1
                stats.note_skip("malformed row length")
                continue

            symbol, volume, open_, close, high, low, ns_raw, txns = row

            sec_id = symbol_ids.get(symbol)
            if sec_id is None:
                stats.skipped_symbol += 1
                stats.note_skip("symbol not resolvable")
                continue

            try:
                ns = int(ns_raw)
                if not (NS_MIN <= ns <= NS_MAX):
                    raise ValueError("epoch out of range")
                ts = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
                o, h, low_v, c = open_, high, low, close
                # Bar self-consistency is enforced by CHECK constraints on the table. Screening the
                # obvious violations here keeps one corrupt row from aborting a 1.4M-row COPY, and
                # counts it with a reason instead.
                if float(low_v) <= 0 or float(h) < float(low_v):
                    raise ValueError("inconsistent OHLC")
                if not (float(low_v) <= float(o) <= float(h)) or not (float(low_v) <= float(c) <= float(h)):
                    raise ValueError("open/close outside high-low")
                vol = int(volume)
                if vol < 0:
                    raise ValueError("negative volume")
                txn = int(txns) if txns not in ("", None) else None
            except (ValueError, TypeError) as exc:
                stats.skipped_bad_row += 1
                stats.note_skip(str(exc)[:60])
                continue

            if stats.min_ts is None or ts < stats.min_ts:
                stats.min_ts = ts
            if stats.max_ts is None or ts > stats.max_ts:
                stats.max_ts = ts

            stats.rows_written += 1
            yield (sec_id, ts, o, h, low_v, c, vol, txn, source_id)
    finally:
        fh.close()


def load_file(conn: psycopg.Connection, path: Path, *, dry_run: bool) -> FileStats | None:
    """Load one day file. Returns None if it was already loaded (idempotent skip)."""
    stats = FileStats(path=path)
    started = time.monotonic()

    digest = sha256_of(path)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, row_count FROM data_sources WHERE provider = %s AND dataset = %s AND source_sha256 = %s",
            (PROVIDER, DATASET, digest),
        )
        existing = cur.fetchone()
    if existing:
        logger.info("skip %s — identical bytes already loaded (source_id=%s, %s rows)", path.name, existing[0], existing[1])
        return None

    symbols, lo, hi, approx_rows = scan_file(path)
    stats.min_ts, stats.max_ts = lo, hi

    if dry_run:
        stats.rows_read = approx_rows
        stats.seconds = time.monotonic() - started
        logger.info(
            "would load %s — %s rows, %s symbols, %s → %s",
            path.name, f"{approx_rows:,}", f"{len(symbols):,}", lo.isoformat(), hi.isoformat(),
        )
        return stats

    # One transaction per file: the data_sources row, the securities upserts, the partitions, and
    # every bar commit together or not at all. A half-loaded day that still counts as "loaded" is
    # exactly what the resume logic must never see.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO data_sources (provider, dataset, fetched_at, period_start, period_end, "
                "source_sha256, source_uri, row_count, notes) "
                "VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s) RETURNING id",
                (PROVIDER, DATASET, lo.date(), hi.date(), digest, str(path), approx_rows,
                 "Polygon flat-file minute aggregates"),
            )
            source_id = cur.fetchone()[0]

        symbol_ids = resolve_symbols(conn, symbols, source_id, lo.date())
        unresolved = len(symbols) - len(symbol_ids)
        if unresolved:
            logger.warning("%s: %d symbol(s) did not match the reference grammar and are skipped", path.name, unresolved)

        made = ensure_partitions(conn, lo, hi)
        logger.debug("%s: partitions ensured %s", path.name, made)

        with conn.cursor() as cur:
            with cur.copy(
                "COPY price_bars_minute (security_id, ts, open, high, low, close, volume, transactions, source_id) "
                "FROM STDIN"
            ) as cp:
                for record in _rows(path, symbol_ids, source_id, stats):
                    cp.write_row(record)

            # row_count is written before the rows are counted, so correct it now that we know.
            cur.execute("UPDATE data_sources SET row_count = %s WHERE id = %s", (stats.rows_written, source_id))

    stats.seconds = time.monotonic() - started
    return stats


def connect_from_env() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LoadError("DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-load-bars")
    with conn.cursor() as cur:
        # A 1.4M-row COPY plus partition DDL can exceed any sane statement timeout, and the load's
        # protection is the per-file transaction, not a clock.
        cur.execute("SET statement_timeout = 0")
        cur.execute("SET idle_in_transaction_session_timeout = 0")
        # Bulk COPY into a partitioned table sorts and builds index entries; the default 16 MB
        # makes that spill to disk unnecessarily. Session-scoped, so nothing else is affected.
        cur.execute("SET maintenance_work_mem = '256MB'")
    return conn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_minute_bars", description="Load Polygon minute bars into Postgres")
    p.add_argument("--root", type=Path, default=Path("data/market/minute_bars_5y"))
    p.add_argument("--limit", type=int, help="load at most N files (smoke tests)")
    p.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        files = discover_files(args.root)
    except LoadError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION

    if args.limit:
        files = files[: args.limit]
    logger.info("%d file(s) to consider under %s", len(files), args.root)

    try:
        conn = connect_from_env()
    except LoadError as exc:
        logger.error("%s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("could not connect: %s", exc)
        return EXIT_CONNECTION

    total_rows = 0
    total_skipped = 0
    loaded = skipped_files = 0
    started = time.monotonic()

    try:
        for i, path in enumerate(files, 1):
            try:
                stats = load_file(conn, path, dry_run=args.dry_run)
            except psycopg.Error as exc:
                # The file's transaction rolled back, so nothing partial is recorded and a re-run
                # retries this file. Stop rather than continue: a systematic fault would otherwise
                # produce 1,200 identical failures.
                logger.error("%s: %s", path.name, exc)
                return EXIT_SQL
            except LoadError as exc:
                logger.error("%s: %s", path.name, exc)
                return EXIT_VALIDATION

            if stats is None:
                skipped_files += 1
                continue

            loaded += 1
            total_rows += stats.rows_written
            total_skipped += stats.skipped_symbol + stats.skipped_bad_row
            rate = stats.rows_written / stats.seconds if stats.seconds else 0
            logger.info(
                "[%d/%d] %s — %s rows in %.1fs (%s rows/s)%s",
                i, len(files), path.name, f"{stats.rows_written:,}", stats.seconds, f"{rate:,.0f}",
                f", {stats.skipped_symbol + stats.skipped_bad_row} skipped" if (stats.skipped_symbol or stats.skipped_bad_row) else "",
            )
            if stats.skipped_reasons:
                logger.debug("  skip reasons: %s", stats.skipped_reasons)
    finally:
        conn.close()

    elapsed = time.monotonic() - started
    logger.info(
        "done — %d file(s) loaded, %d already present, %s rows written, %s skipped, %.1fs (%s rows/s)",
        loaded, skipped_files, f"{total_rows:,}", f"{total_skipped:,}", elapsed,
        f"{total_rows / elapsed:,.0f}" if elapsed else "n/a",
    )
    # Skipped rows are reported, never silent. A load that quietly drops 3% of the universe would
    # produce a backtest nobody could explain.
    if total_skipped:
        logger.warning("%s row(s) were skipped — see the per-file reasons above", f"{total_skipped:,}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
