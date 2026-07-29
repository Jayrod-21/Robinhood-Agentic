"""Load the market calendar and the risk-free rate series.

Two small reference tables that a great deal waits on. Every Sharpe ratio needs a risk-free rate, and
`n_observations` counts sessions rather than calendar days — so without these, no risk-adjusted metric
can be computed at all.

THE CALENDAR MUST NOT BE DERIVED FROM DATA COVERAGE
    The tempting shortcut is "a date with bars is a trading day". It is wrong, and wrong in the
    dangerous direction. This archive is missing 2024-12-10 through 2024-12-31 — fifteen genuine
    trading days lost to corrupt gzip members. Deriving the calendar from bar presence would mark
    them `is_trading_day = false`, i.e. record that the market was CLOSED for the second half of
    December 2024. The data gap would then be invisible: every consumer would treat it as a holiday
    stretch, `n_observations` would look correct, and no return series would appear to have a hole.

    So the calendar records what the MARKET did, from exchange rules. Coverage is a separate question,
    answered by joining to `price_bars_daily` — and `report` does exactly that, so a gap stays a gap.

    Session times come from the exchange RULES, not from the bars. Deriving them empirically was the
    original plan — a 13:00 ET half-day shows up as a last bar at 12:59 — but it does not work here:
    the minute table holds only a handful of days, and the join over 12.8M daily rows exhausted
    Postgres's shared memory while contributing nothing, because the result was computed and then
    never used. The early-close rules cover the same half-days deterministically, so this is a
    simpler mechanism reaching the same answer. `report` is what confirms the rules match reality.

RISK-FREE RATE
    FRED's DTB3 (3-Month Treasury Bill, secondary market, daily). Fetched from the CSV endpoint,
    which needs no API key.

    `known_at` is set to the day AFTER `effective_date`, because FRED publishes with a lag and the
    real release timestamp is not in the CSV. That is an APPROXIMATION and is recorded as such: a
    genuine point-in-time claim needs actual release times, and until then a backtest reading rates
    at same-day resolution is very slightly optimistic. The error is a single day on a rate that
    moves in basis points, which is immaterial next to the equity returns it offsets — but it is a
    known approximation rather than an unexamined one.

    Rates are stored as FRACTIONS (0.0525 for 5.25%), matching the table's ±1 CHECK.

Usage:
    python db/load_reference_data.py calendar --from 2020-01-01 --to 2026-12-31
    python db/load_reference_data.py rates
    python db/load_reference_data.py report
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    print("load_reference_data: psycopg (v3) required", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("reference_data")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

EXCHANGE_TZ = ZoneInfo("America/New_York")
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
EARLY_CLOSE = dtime(13, 0)

FRED_SERIES = "DTB3"
FRED_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_SERIES}"


class LoadError(Exception):
    """A failure raised deliberately."""


# ── NYSE holiday rules ────────────────────────────────────────────────────────────────────────
# Unscheduled closures — these cannot be derived from any rule and must be listed. Folded into
# nyse_holidays() so that function is the single authority on whether the market was shut.
AD_HOC_CLOSURES: set[date] = {
    date(2025, 1, 9),   # National Day of Mourning, President Carter
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month (weekday: Monday=0)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last given weekday of a month."""
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Needed only to locate Good Friday."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lu = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lu) // 451
    month = (h + lu - 7 * m + 114) // 31
    day = ((h + lu - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE observance: a Saturday holiday is taken the preceding Friday, a Sunday the following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _observed_new_year(d: date) -> date:
    """New Year's Day observance, which is NOT the general rule.

    The NYSE exempts New Year's Day from the Saturday→preceding-Friday shift: when 1 January falls on
    a Saturday, the market trades normally on the final Friday of December. Only the
    Sunday→following-Monday shift applies.

    This was not a theoretical distinction. Applying the general rule marked Friday 2021-12-31 closed
    (because 2022-01-01 was a Saturday) while the archive holds 10,871 bars for that session — caught
    by the coverage report's "has bars but is not a trading day" check, which exists precisely to test
    these rules against reality rather than trusting them.
    """
    if d.weekday() == 6:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return d  # returns a Saturday, which is not a weekday and so never becomes a trading day
    return d


def nyse_holidays(year: int) -> set[date]:
    """Full-day NYSE closures for one year."""
    hs = {
        _observed_new_year(date(year, 1, 1)),        # New Year's Day — special observance
        _nth_weekday(year, 1, 0, 3),                 # MLK Day
        _nth_weekday(year, 2, 0, 3),                 # Washington's Birthday
        _easter(year) - timedelta(days=2),           # Good Friday
        _last_weekday(year, 5, 0),                   # Memorial Day
        _observed(date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                 # Labor Day
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving
        _observed(date(year, 12, 25)),               # Christmas
    }
    # Juneteenth became an NYSE holiday in 2022, not before.
    if year >= 2022:
        hs.add(_observed(date(year, 6, 19)))
    # Fold in unscheduled closures for this year, so this function is the SINGLE authority on
    # whether the market was shut. Applying them only at the call site meant any other caller got a
    # quietly incomplete answer — which a rules test caught immediately.
    hs |= {d for d in AD_HOC_CLOSURES if d.year == year}
    return hs


def nyse_early_closes(year: int) -> set[date]:
    """Scheduled 13:00 ET closes."""
    out: set[date] = set()
    # Day after Thanksgiving.
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))
    # July 3 when Independence Day falls on a weekday other than Monday.
    jul4 = date(year, 7, 4)
    if jul4.weekday() in (1, 2, 3, 4):
        out.add(date(year, 7, 3))
    # Christmas Eve when Christmas falls Tue-Fri.
    dec25 = date(year, 12, 25)
    if dec25.weekday() in (1, 2, 3, 4):
        out.add(date(year, 12, 24))
    return out


def connect() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LoadError("DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-reference")
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
    return conn


# ── calendar ──────────────────────────────────────────────────────────────────────────────────
def cmd_calendar(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    lo = date.fromisoformat(args.date_from)
    hi = date.fromisoformat(args.date_to)
    if hi < lo:
        raise LoadError(f"--to ({hi}) is before --from ({lo})")

    rows = []
    holidays: set[date] = set()
    early: set[date] = set()
    for y in range(lo.year, hi.year + 1):
        holidays |= nyse_holidays(y)
        early |= nyse_early_closes(y)

    d = lo
    n_trading = n_closed = 0
    while d <= hi:
        weekend = d.weekday() >= 5
        is_trading = not weekend and d not in holidays
        if is_trading:
            close_t = EARLY_CLOSE if d in early else REGULAR_CLOSE
            open_dt = datetime.combine(d, REGULAR_OPEN, tzinfo=EXCHANGE_TZ)
            close_dt = datetime.combine(d, close_t, tzinfo=EXCHANGE_TZ)
            rows.append((d, True, open_dt, close_dt))
            n_trading += 1
        else:
            rows.append((d, False, None, None))
            n_closed += 1
        d += timedelta(days=1)

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO market_calendar (trade_date, is_trading_day, session_open, session_close)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (trade_date) DO UPDATE
               SET is_trading_day = EXCLUDED.is_trading_day,
                   session_open   = EXCLUDED.session_open,
                   session_close  = EXCLUDED.session_close
            """,
            rows,
        )

    logger.info("calendar %s → %s: %d trading days, %d closed", lo, hi, n_trading, n_closed)
    logger.info("%d scheduled early close(s), %d ad-hoc closure(s) applied", len(early), len(AD_HOC_CLOSURES))
    return EXIT_OK


# ── rates ─────────────────────────────────────────────────────────────────────────────────────
def cmd_rates(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    logger.info("fetching %s from FRED", FRED_SERIES)
    try:
        with urllib.request.urlopen(FRED_URL, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LoadError(f"FRED fetch failed: {exc}") from exc

    reader = csv.DictReader(io.StringIO(payload))
    date_col = reader.fieldnames[0] if reader.fieldnames else None
    if not date_col or FRED_SERIES not in (reader.fieldnames or []):
        raise LoadError(f"unexpected FRED columns {reader.fieldnames!r}")

    rows = []
    skipped = 0
    for rec in reader:
        raw = (rec.get(FRED_SERIES) or "").strip()
        # FRED writes "." for a non-publication day (holidays, and days before the series began).
        if raw in ("", "."):
            skipped += 1
            continue
        try:
            eff = date.fromisoformat(rec[date_col].strip())
            pct = float(raw)
        except ValueError:
            skipped += 1
            continue
        # FRED publishes percent; the column is a fraction with a ±1 CHECK.
        rate = pct / 100.0
        # See the module docstring: an approximation, not a real release timestamp.
        known = datetime.combine(eff + timedelta(days=1), dtime(0, 0), tzinfo=timezone.utc)
        rows.append((FRED_SERIES, eff, rate, known))

    if not rows:
        raise LoadError("FRED returned no usable observations")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, period_start, period_end, "
            "source_uri, row_count, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            ("fred", "risk_free_rates", datetime.now(timezone.utc), rows[0][1], rows[-1][1],
             FRED_URL, len(rows),
             "FRED DTB3 3-month T-bill, secondary market. known_at approximated as effective_date+1d."),
        )
        source_id = cur.fetchone()[0]

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO risk_free_rates (series, effective_date, annual_rate, known_at, source_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (series, effective_date, known_at) DO NOTHING",
            [(s, e, r, k, source_id) for s, e, r, k in rows],
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(effective_date), max(effective_date), "
            "round(min(annual_rate)*100,3), round(max(annual_rate)*100,3) "
            "FROM risk_free_rates WHERE series=%s",
            (FRED_SERIES,),
        )
        n, lo, hi, rmin, rmax = cur.fetchone()
    logger.info("rates: %s observations %s → %s, %s%% to %s%% (%d non-publication days skipped)",
                f"{n:,}", lo, hi, rmin, rmax, skipped)
    return EXIT_OK


# ── coverage report ───────────────────────────────────────────────────────────────────────────
def cmd_report(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    """Join the calendar against the bars, so a data gap reads as a gap and not a holiday."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(trade_date), max(trade_date) FROM price_bars_daily")
        lo, hi = cur.fetchone()
        if lo is None:
            logger.warning("no daily bars loaded")
            return EXIT_OK

        cur.execute(
            """
            SELECT c.trade_date
            FROM market_calendar c
            WHERE c.is_trading_day
              AND c.trade_date BETWEEN %s AND %s
              AND NOT EXISTS (SELECT 1 FROM price_bars_daily d WHERE d.trade_date = c.trade_date)
            ORDER BY c.trade_date
            """,
            (lo, hi),
        )
        missing = [r[0] for r in cur.fetchall()]

        cur.execute(
            """
            SELECT count(*) FROM (
                SELECT DISTINCT d.trade_date FROM price_bars_daily d
                LEFT JOIN market_calendar c ON c.trade_date = d.trade_date
                WHERE c.trade_date IS NULL OR NOT c.is_trading_day
            ) x
            """
        )
        unexpected = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM market_calendar WHERE is_trading_day AND trade_date BETWEEN %s AND %s", (lo, hi))
        expected = cur.fetchone()[0]

    logger.info("coverage %s → %s: %d trading days expected, %d missing", lo, hi, expected, len(missing))
    if missing:
        logger.warning("%d TRADING DAY(S) HAVE NO BARS — these are data gaps, not holidays:", len(missing))
        for d in missing[:25]:
            logger.warning("    %s", d)
        if len(missing) > 25:
            logger.warning("    … and %d more", len(missing) - 25)
    else:
        logger.info("every expected trading day has bars")
    if unexpected:
        # Bars on a day the calendar calls closed means the calendar is wrong — a missed ad-hoc
        # closure, or a holiday rule that does not hold for that year.
        logger.warning("%d date(s) have bars but are NOT marked trading days — the calendar is wrong", unexpected)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_reference_data")
    p.add_argument("command", choices=("calendar", "rates", "report"))
    p.add_argument("--from", dest="date_from", default="2020-01-01")
    p.add_argument("--to", dest="date_to", default="2026-12-31")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        conn = connect()
    except LoadError as exc:
        logger.error("%s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("could not connect: %s", exc)
        return EXIT_CONNECTION
    try:
        return {"calendar": cmd_calendar, "rates": cmd_rates, "report": cmd_report}[args.command](conn, args)
    except LoadError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.Error as exc:
        logger.error("database error: %s", exc)
        return EXIT_SQL
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
