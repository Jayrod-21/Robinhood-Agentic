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

CORRUPT MEMBERS ARE SKIPPED AND REPORTED, NEVER FATAL
    The archive contains 15 known-corrupt gzip members (2024-12-10 → 2024-12-31): 14 fail at the
    first read ("Not a gzipped file") and one (2024-12-10) inflates for 1,266,147 data rows
    (plus the header line) before a bad deflate block. Corruption therefore surfaces lazily, mid-iteration — including inside the COPY,
    where the per-file transaction rolls the partial load back cleanly. Each corrupt file is
    reported by name, the run continues, and the final exit is EXIT_VALIDATION so an incomplete
    archive can never read as success. Same machinery and same 15 files as load_daily_bars.py.

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
import io
import logging
import os
import re
import sys
import time
import zlib
from collections.abc import Iterator
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


class CorruptArchive(LoadError):
    """A gzip member that cannot be read.

    Its own type because it means something different from every other failure: the DATA is bad,
    not the code or the database. A run must report it, skip the file, and carry on — the archive
    this loader exists to load contains 15 known-corrupt members (2024-12-10 → 2024-12-31), and
    crashing on the first would hide the other 14 behind a traceback. Same machinery as
    load_daily_bars.py, which met these files first.
    """


# Everything a corrupt-but-present gzip member can raise while being read as CSV text:
#   OSError            — gzip.BadGzipFile (bad magic / CRC) and genuine read errors alike
#   EOFError           — a truncated member
#   zlib.error         — an invalid deflate stream mid-member (2024-12-10.csv.gz raises this
#                        after 1,266,148 good rows — corruption surfaces lazily, mid-iteration)
#   UnicodeDecodeError — the stream inflates but the bytes are not UTF-8; a ValueError subclass,
#                        NOT an OSError, so it must be listed explicitly
#   csv.Error          — inflated garbage that decodes as text can produce a field beyond
#                        csv.field_size_limit(), raised from next(reader)
CORRUPT_STREAM_ERRORS = (OSError, EOFError, zlib.error, UnicodeDecodeError, csv.Error)


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
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as exc:
        # A vanished or unreadable file gets the same report-skip-continue channel as a corrupt
        # one: the operator needs the filename and the reason, not a traceback.
        raise CorruptArchive(f"{path.name}: cannot read file bytes: {exc}") from exc
    return h.hexdigest()


def discover_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise LoadError(f"data root not found: {root}")
    files = sorted(root.rglob("*.csv.gz"))
    if not files:
        raise LoadError(f"no .csv.gz files under {root}")
    return files


def _open_csv(path: Path) -> tuple[io.TextIOBase, Iterator[list[str]]]:
    """Open a gzipped CSV and consume its header. Raises CorruptArchive for an unreadable member.

    gzip.open is lazy: a bad magic number surfaces at the first read, not at open — so the header
    read is guarded too, and gzip.BadGzipFile subclasses OSError rather than zlib.error.
    """
    try:
        fh = gzip.open(path, "rt", newline="", encoding="utf-8")
    except CORRUPT_STREAM_ERRORS as exc:
        raise CorruptArchive(f"{path.name}: cannot open as gzip: {exc}") from exc
    reader = csv.reader(fh)
    try:
        header = next(reader, None)
    except CORRUPT_STREAM_ERRORS as exc:
        fh.close()
        raise CorruptArchive(f"{path.name}: corrupt gzip stream at header: {exc}") from exc
    if header != EXPECTED_HEADER:
        fh.close()
        raise LoadError(
            f"{path.name}: unexpected header {header!r}. Expected {EXPECTED_HEADER!r} — the "
            "provider's column set changed and the parser must be reviewed before loading."
        )
    return fh, reader


def _rows_or_corrupt(reader: Iterator[list[str]], path: Path) -> Iterator[list[str]]:
    """Yield CSV rows, converting a mid-stream decompression failure into CorruptArchive.

    gzip surfaces corruption lazily — a member can decompress for hundreds of thousands of rows
    and then fail on a bad block, so the failure must be caught around the ITERATION, not the open.
    """
    while True:
        try:
            yield next(reader)
        except StopIteration:
            return
        except CORRUPT_STREAM_ERRORS as exc:
            raise CorruptArchive(f"{path.name}: corrupt gzip stream mid-file: {exc}") from exc


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
        for row in _rows_or_corrupt(reader, path):
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
    """Second pass: yield COPY-ready tuples. Generator — one row in memory at a time.

    A CorruptArchive raised here propagates out of the COPY inside load_file's transaction block,
    which rolls the whole file back — provenance row and bars together — before main skips the
    file. That rollback path is load-bearing: keep the exception propagating out of
    `with conn.transaction()`, never swallowed inside it.
    """
    fh, reader = _open_csv(path)
    try:
        for row in _rows_or_corrupt(reader, path):
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
        # Do not wait for WAL fsync on each commit. This weakens durability ONLY for this session:
        # an OS crash could lose recently-committed files. That is acceptable precisely because the
        # loader is resumable by content hash — a lost file is detected as absent and re-loaded,
        # whereas a lost row would not be. Do not copy this setting into anything that writes
        # account or order state, where a silently-lost commit is unrecoverable.
        cur.execute("SET synchronous_commit = off")
    return conn


def positive_int(raw: str) -> int:
    """argparse type: an int >= 1. `--limit 0` used to silently mean 'no limit' via falsiness."""
    v = int(raw)
    if v < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {v}")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_minute_bars", description="Load Polygon minute bars into Postgres")
    p.add_argument("--root", type=Path, default=Path("data/market/minute_bars_5y"))
    p.add_argument("--limit", type=positive_int, help="load at most N files (smoke tests)")
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
    corrupt: list[str] = []
    started = time.monotonic()

    try:
        for i, path in enumerate(files, 1):
            try:
                stats = load_file(conn, path, dry_run=args.dry_run)
            except psycopg.OperationalError as exc:
                # The connection died (server restart, network drop) — an infrastructure failure,
                # not a SQL one, so it maps to the connection exit code.
                logger.error("%s: connection lost: %s", path.name, exc)
                return EXIT_CONNECTION
            except psycopg.Error as exc:
                # The file's transaction rolled back, so nothing partial is recorded and a re-run
                # retries this file. Stop rather than continue: a systematic fault would otherwise
                # produce 1,200 identical failures.
                logger.error("%s: %s", path.name, exc)
                return EXIT_SQL
            except CorruptArchive as exc:
                # The DATA is bad, not the code. If it surfaced mid-COPY the file's transaction
                # rolled back, so nothing partial is recorded. Report it, count it, keep going —
                # one unreadable file must not hide the state of the other 1,255.
                corrupt.append(path.name)
                logger.error("CORRUPT %s", exc)
                continue
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
    if corrupt:
        # Non-zero exit: the archive is incomplete and every downstream date range is affected.
        # Silence here would let a gap in the price series look like a market holiday.
        logger.error("%d CORRUPT file(s) could not be read: %s", len(corrupt), ", ".join(corrupt))
        logger.error("Those trading days are ABSENT from the minute series. Re-copy them from the "
                     "source drive and re-run; the loader resumes by content hash.")
        return EXIT_VALIDATION
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
