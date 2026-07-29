"""Derive daily OHLCV bars from the Polygon minute archive.

WHY DERIVE RATHER THAN LOAD MINUTES AND AGGREGATE IN SQL
    The evaluation framework computes Sharpe and Sortino on DAILY returns (EVALUATION_FRAMEWORK §2),
    so daily bars are on the critical path and minute bars are not. Loading the full minute archive
    is ~2 billion rows, ~25 hours, and ~290 GB — measured, and bottlenecked on index maintenance
    rather than WAL (an unindexed COPY runs 150k rows/s against 24k indexed). Deriving daily
    directly from the same files is ~11 million rows and roughly an hour, and unblocks the marking
    job, every metric, and the backtest.

    Minute bars remain valuable for intraday microstructure features. That is a later phase, and
    this loader does not depend on them being present.

THE SESSION DECISION — the one that changes the numbers
    Polygon day files span 04:00-20:00 ET: pre-market, regular session, and post-market. A daily bar
    built from ALL of them would open at the 04:00 pre-market print, which matches no standard daily
    series anywhere and would silently disagree with every other source we might reconcile against.

    So a daily bar here is the REGULAR SESSION only — 09:30:00 to 15:59:59 ET — which is the
    convention every daily OHLCV feed uses:

        open   = the open of the first regular-session minute
        high   = max high across regular-session minutes
        low    = min low across regular-session minutes
        close  = the close of the LAST regular-session minute
        volume = sum of regular-session volume

    Extended-hours activity is deliberately excluded, not lost — it remains in the minute archive.

    The session boundary is computed in America/New_York, so it follows DST rather than assuming a
    fixed UTC offset. That matters: the same archive already proved that a fixed-offset assumption
    about ET breaks at month ends (see load_minute_bars.py). 09:30 ET is 13:30 UTC in summer and
    14:30 UTC in winter.

    `adj_close` is left NULL. Nothing in this project computes split/dividend adjustment yet — see
    issue #35. Until it does, returns derived from `close` are UNADJUSTED and wrong across any split.
    Leaving the column NULL rather than copying `close` into it keeps that gap visible instead of
    burying an unadjusted number in a field whose name promises otherwise.

MEMORY
    One day at a time, one accumulator per symbol (~9,000-10,400 of them). Trivial next to the 1.6M
    minute rows the file holds, which are streamed and discarded.

Usage:
    python db/load_daily_bars.py --root data/market/minute_bars_5y
    python db/load_daily_bars.py --root … --limit 20 --dry-run
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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - clear message beats a traceback
    print("load_daily_bars: psycopg (v3) is required. pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("load_daily_bars")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

PROVIDER = "polygon"
DATASET = "daily_bars"

EXCHANGE_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = dtime(9, 30)
SESSION_LAST_MINUTE = dtime(15, 59)  # bars are stamped at their OPEN, so 15:59 is the final one

SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}(\.[A-Za-z0-9]{1,4}){0,2}$")
NS_MIN = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
NS_MAX = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
EXPECTED_HEADER = ["ticker", "volume", "open", "close", "high", "low", "window_start", "transactions"]


class LoadError(Exception):
    """A failure this module raises deliberately."""


@dataclass(slots=True)
class DayBar:
    """Accumulator for one symbol's regular session.

    Timestamps are held as raw nanosecond epochs, not datetimes: they are only ever compared, and
    ~9,000 of these exist per file. `slots=True` because they are the one structure that scales with
    the universe.
    """
    first_ns: int
    last_ns: int
    open: str
    high: float
    low: float
    close: str
    volume: int

    def update(self, ns: int, o: str, h: float, low: float, c: str, v: int) -> None:
        # Bars can arrive out of order, so open and close track the extreme TIMESTAMPS rather than
        # first-and-last-seen. Assuming file order would be a silent correctness bug.
        if ns < self.first_ns:
            self.first_ns, self.open = ns, o
        if ns > self.last_ns:
            self.last_ns, self.close = ns, c
        if h > self.high:
            self.high = h
        if low < self.low:
            self.low = low
        self.volume += v


def sha256_of(path: Path) -> str:
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


FILENAME_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv\.gz$")


def session_bounds_ns(trade_date: date) -> tuple[int, int]:
    """UTC nanosecond bounds of the regular session for one ET trading date.

    Computed ONCE per file rather than per row. The regular session never crosses ET midnight, so a
    whole day's membership test reduces to an integer range check — which removes ~1.4 million
    `astimezone()` calls and `datetime` constructions per file, the dominant cost of the first
    implementation.

    The conversion still goes through `America/New_York`, so DST is handled by the zone rather than
    assumed: 09:30 ET is 13:30 UTC in summer and 14:30 UTC in winter.
    """
    start_et = datetime.combine(trade_date, SESSION_OPEN, tzinfo=EXCHANGE_TZ)
    # Inclusive of the final minute's bar, which is stamped at its open (15:59).
    end_et = datetime.combine(trade_date, SESSION_LAST_MINUTE, tzinfo=EXCHANGE_TZ)
    return (
        int(start_et.timestamp() * 1_000_000_000),
        int(end_et.timestamp() * 1_000_000_000),
    )


def trade_date_from_name(path: Path) -> date:
    m = FILENAME_DATE_RE.search(path.name)
    if not m:
        raise LoadError(f"{path.name}: filename does not carry a YYYY-MM-DD date")
    return date.fromisoformat(m.group(1))


def aggregate_file(path: Path) -> tuple[dict[tuple[str, date], DayBar], int, int]:
    """Stream one day file and fold it into per-symbol regular-session bars."""
    bars: dict[tuple[str, date], DayBar] = {}
    rows_read = skipped = 0

    trade_date = trade_date_from_name(path)
    lo_ns, hi_ns = session_bounds_ns(trade_date)

    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != EXPECTED_HEADER:
            raise LoadError(
                f"{path.name}: unexpected header {header!r}; expected {EXPECTED_HEADER!r} — the "
                "provider's column set changed and the parser must be reviewed before loading."
            )

        for row in reader:
            rows_read += 1
            if len(row) != 8:
                skipped += 1
                continue
            symbol, volume, open_, close, high, low, ns_raw, _txn = row

            # Cheapest discriminating test first: most rows in the file are extended-hours and are
            # rejected here without any parsing, validation, or object construction.
            try:
                ns = int(ns_raw)
            except ValueError:
                skipped += 1
                continue
            if not (lo_ns <= ns <= hi_ns):
                continue  # pre/post market — excluded by design, not an error

            if not SYMBOL_RE.match(symbol):
                skipped += 1
                continue
            try:
                h, low_v = float(high), float(low)
                if low_v <= 0 or h < low_v:
                    raise ValueError("inconsistent high/low")
                v = int(volume)
                if v < 0:
                    raise ValueError("negative volume")
            except (ValueError, TypeError):
                skipped += 1
                continue

            key = (symbol, trade_date)
            existing = bars.get(key)
            if existing is None:
                # ns is kept as the ordering key rather than a datetime: only comparisons are needed,
                # and constructing ~500k datetimes per file to compare them would be wasteful.
                bars[key] = DayBar(ns, ns, open_, h, low_v, close, v)
            else:
                existing.update(ns, open_, h, low_v, close, v)

    return bars, rows_read, skipped


def resolve_symbols(conn: psycopg.Connection, symbols: set[str], source_id: int, seen_on: date) -> dict[str, int]:
    valid = sorted(symbols)
    if not valid:
        return {}
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO securities (symbol, first_seen, source_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (symbol) WHERE delisted_at IS NULL DO NOTHING",
            [(s, seen_on, source_id) for s in valid],
        )
        cur.execute(
            "SELECT symbol, id FROM securities WHERE delisted_at IS NULL AND symbol = ANY(%s)",
            (valid,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def load_file(conn: psycopg.Connection, path: Path, *, dry_run: bool) -> tuple[int, int, float] | None:
    """Aggregate and load one day file. Returns None if already loaded."""
    started = time.monotonic()
    digest = sha256_of(path)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, row_count FROM data_sources WHERE provider=%s AND dataset=%s AND source_sha256=%s",
            (PROVIDER, DATASET, digest),
        )
        if (existing := cur.fetchone()) is not None:
            logger.info("skip %s — already derived (source_id=%s, %s bars)", path.name, existing[0], existing[1])
            return None

    bars, rows_read, skipped = aggregate_file(path)
    if not bars:
        raise LoadError(f"{path.name}: no regular-session bars found — refusing to record an empty day")

    symbols = {sym for sym, _ in bars}
    trade_dates = {d for _, d in bars}

    if dry_run:
        el = time.monotonic() - started
        logger.info(
            "would derive %s — %s daily bars from %s minute rows, %s symbols, date(s) %s",
            path.name, f"{len(bars):,}", f"{rows_read:,}", f"{len(symbols):,}",
            ", ".join(sorted(str(d) for d in trade_dates)),
        )
        return len(bars), skipped, el

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO data_sources (provider, dataset, fetched_at, period_start, period_end, "
                "source_sha256, source_uri, row_count, notes) VALUES (%s,%s,now(),%s,%s,%s,%s,%s,%s) RETURNING id",
                (PROVIDER, DATASET, min(trade_dates), max(trade_dates), digest, str(path), len(bars),
                 "Derived from Polygon minute aggregates: regular session 09:30-15:59 ET"),
            )
            source_id = cur.fetchone()[0]

        symbol_ids = resolve_symbols(conn, symbols, source_id, min(trade_dates))

        written = 0
        with conn.cursor() as cur:
            with cur.copy(
                "COPY price_bars_daily (security_id, trade_date, open, high, low, close, volume, source_id) FROM STDIN"
            ) as cp:
                for (symbol, trade_date), b in bars.items():
                    sec_id = symbol_ids.get(symbol)
                    if sec_id is None:
                        skipped += 1
                        continue
                    # The table's CHECK requires open and close within [low, high]. A provider row
                    # violating that would abort the COPY, so drop and count it instead.
                    o, c = float(b.open), float(b.close)
                    if not (b.low <= o <= b.high) or not (b.low <= c <= b.high):
                        skipped += 1
                        continue
                    cp.write_row((sec_id, trade_date, b.open, b.high, b.low, b.close, b.volume, source_id))
                    written += 1

            cur.execute("UPDATE data_sources SET row_count=%s WHERE id=%s", (written, source_id))

    return written, skipped, time.monotonic() - started


def connect_from_env() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LoadError("DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-load-daily")
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute("SET idle_in_transaction_session_timeout = 0")
        # Durability is traded for throughput only here: the loader is resumable by content hash, so
        # a lost commit is detected as an absent file and re-derived. Never copy this into anything
        # writing account or order state, where a silently-lost commit is unrecoverable.
        cur.execute("SET synchronous_commit = off")
    return conn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_daily_bars", description="Derive daily bars from the minute archive")
    p.add_argument("--root", type=Path, default=Path("data/market/minute_bars_5y"))
    p.add_argument("--limit", type=int, help="process at most N files")
    p.add_argument("--dry-run", action="store_true", help="aggregate and report; write nothing")
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

    total = total_skipped = derived = already = 0
    started = time.monotonic()
    try:
        for i, path in enumerate(files, 1):
            try:
                result = load_file(conn, path, dry_run=args.dry_run)
            except psycopg.Error as exc:
                logger.error("%s: %s", path.name, exc)
                return EXIT_SQL
            except LoadError as exc:
                logger.error("%s: %s", path.name, exc)
                return EXIT_VALIDATION

            if result is None:
                already += 1
                continue
            written, skipped, el = result
            derived += 1
            total += written
            total_skipped += skipped
            if args.verbose or i % 25 == 0 or i == len(files):
                logger.info("[%d/%d] %s — %s bars in %.1fs", i, len(files), path.name, f"{written:,}", el)
    finally:
        conn.close()

    elapsed = time.monotonic() - started
    logger.info(
        "done — %d file(s) derived, %d already present, %s daily bars, %s minute rows skipped, %.1fs",
        derived, already, f"{total:,}", f"{total_skipped:,}", elapsed,
    )
    if total_skipped:
        logger.warning("%s minute row(s) were skipped — see per-file detail with --verbose", f"{total_skipped:,}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
