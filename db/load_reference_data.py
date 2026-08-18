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
    FRED's DGS3MO (3-Month Treasury, constant maturity, INVESTMENT basis). Fetched from the CSV
    endpoint, which needs no API key.

    WHY DGS3MO AND NOT DTB3: DTB3 is quoted on a DISCOUNT basis (discount from face over
    actual/360); a Sharpe's risk-free leg should be an investment/coupon-equivalent yield, which
    is what DGS3MO is — and what migration 004's own column comment names as the example series.
    Measured over this archive's window (1,250 paired sessions, 2026-07-29): DGS3MO − DTB3
    averages +0.1177 pp, max +0.28 pp, and +0.2292 pp over the 337 sessions with DTB3 >= 5%.
    NOT strictly one direction — 35 sessions are slightly negative (min −0.02 pp) and 348 are
    exactly 0 — but the mean effect is: using DTB3 understates rf and flatters excess returns
    and Sharpe on average (semantics review S-S3). Previously-loaded DTB3 rows remain — series
    is part of the identity — but consumers should read DGS3MO.

    `known_at` semantics (semantics review S-S4):
      * A FIRST observation of an effective_date is stored with known_at = effective_date + 1 day
        at 00:00 UTC. That is CONSERVATIVE, not optimistic: FRED's H.15 publishes ~16:15 ET on the
        effective day, and midnight UTC is 19:00-20:00 ET the same evening — after publication. A
        consumer enforcing known_at <= decision_time will decline to use day D's rate intraday on
        day D, which is the safe direction. This floor keeps historical backfills usable for
        point-in-time reads (a fetch-time known_at on a 60-year backfill would hide every rate
        from every historical decision).
      * A REVISED value (this fetch disagrees with the latest stored value for that date) is
        stored as a NEW row with known_at = the fetch time — the moment we actually learned it.
        Deriving known_at purely from effective_date made revisions collide on the PK and vanish
        under ON CONFLICT DO NOTHING; a revision is exactly the thing the (series, effective_date,
        known_at) identity exists to represent.
      * An UNCHANGED value inserts nothing, so re-runs do not bloat the table.

    Rates are stored as FRACTIONS (0.0525 for 5.25%), matching the table's ±1 CHECK, and travel
    as Decimal end to end — never through float.

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
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from decimal import Decimal, InvalidOperation
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

# Investment-basis 3-month constant maturity — see the module docstring for why not DTB3.
FRED_SERIES = "DGS3MO"
FRED_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_SERIES}"

# Retry budget for the FRED GET (idempotent, so retrying is safe): exponential backoff with
# jitter, bounded by the attempt budget (no per-delay cap — growth stops because attempts do;
# total sleep is in [7, 21) seconds). Four attempts spans transient trouble without stalling a
# scheduled run behind a real outage.
FETCH_ATTEMPTS = 4
FETCH_BACKOFF_BASE_S = 2.0


class LoadError(Exception):
    """A failure raised deliberately."""


class FetchError(LoadError):
    """The network fetch failed after retries — maps to EXIT_CONNECTION, not EXIT_VALIDATION.

    A network failure reporting itself as a validation failure breaks the exit-code contract
    (loaders review S-3): an operator or cron job must be able to tell 'the data was bad' from
    'the wire was down' without reading logs.
    """


# ── NYSE holiday rules ────────────────────────────────────────────────────────────────────────
# Unscheduled closures — these cannot be derived from any rule and must be listed. Folded into
# nyse_holidays() so that function is the single authority on whether the market was shut.
#
# NOTE if the calendar range ever moves earlier than 2020: the 9/11 closures (2001-09-11 → 14)
# and Hurricane Sandy (2012-10-29/30) are outside the current default range and must be added
# here, or every derived series for those weeks will misread closure as missing data.
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
def fetch_fred_csv(url: str, *, attempts: int = FETCH_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> str:
    """GET the FRED CSV with exponential backoff + jitter, bounded by the attempt budget
    (idempotent, so retrying is safe).

    Raises FetchError after the last attempt — mapped to EXIT_CONNECTION by main.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            # SSRF surface: none — the URL is a module constant built from a module constant,
            # scheme fixed https, no user input reaches it.
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < attempts:
                delay = FETCH_BACKOFF_BASE_S * (2 ** (attempt - 1)) * (0.5 + random.random())
                logger.warning("FRED fetch attempt %d/%d failed (%s) — retrying in %.1fs",
                               attempt, attempts, exc, delay)
                sleep(delay)
    raise FetchError(f"FRED fetch failed after {attempts} attempts: {last_exc}") from last_exc


def parse_fred_csv(payload: str, series: str = FRED_SERIES) -> tuple[list[tuple[date, Decimal]], int]:
    """Parse FRED's CSV into (effective_date, fractional Decimal rate) pairs plus a skip count.

    Decimal, never float: the value is money-adjacent and float would introduce artifacts like
    0.015300000000000001 that only column scale used to absorb (loaders review N-1).
    """
    reader = csv.DictReader(io.StringIO(payload))
    date_col = reader.fieldnames[0] if reader.fieldnames else None
    if not date_col or series not in (reader.fieldnames or []):
        raise LoadError(f"unexpected FRED columns {reader.fieldnames!r}")

    rows: list[tuple[date, Decimal]] = []
    skipped = 0
    for rec in reader:
        raw = (rec.get(series) or "").strip()
        # FRED writes "." for a non-publication day (holidays, and days before the series began).
        if raw in ("", "."):
            skipped += 1
            continue
        try:
            eff = date.fromisoformat(rec[date_col].strip())
            # FRED publishes percent; the column is a fraction with a ±1 CHECK.
            rate = Decimal(raw) / 100
        except (ValueError, InvalidOperation):
            skipped += 1
            continue
        rows.append((eff, rate))
    return rows, skipped


def cmd_rates(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    logger.info("fetching %s from FRED", FRED_SERIES)
    fetched_at = datetime.now(timezone.utc)
    payload = fetch_fred_csv(FRED_URL)
    observations, skipped = parse_fred_csv(payload)
    if not observations:
        raise LoadError("FRED returned no usable observations")

    # The latest stored value per effective_date, so a revision is DETECTED rather than colliding
    # on the PK and vanishing under ON CONFLICT DO NOTHING (see the module docstring).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (effective_date) effective_date, annual_rate "
            "FROM risk_free_rates WHERE series = %s ORDER BY effective_date, known_at DESC",
            (FRED_SERIES,),
        )
        latest: dict[date, Decimal] = {r[0]: r[1] for r in cur.fetchall()}

    new_rows: list[tuple[date, Decimal, datetime]] = []
    revised = 0
    for eff, rate in observations:
        stored = latest.get(eff)
        if stored is None:
            # First observation: the publication-lag floor (eff + 1d, 00:00 UTC) — conservative,
            # and what keeps a historical backfill usable for point-in-time reads.
            known = datetime.combine(eff + timedelta(days=1), dtime(0, 0), tzinfo=timezone.utc)
            new_rows.append((eff, rate, known))
        elif rate.quantize(Decimal("0.000001")) != stored.quantize(Decimal("0.000001")):
            # Revision: a new row at the moment we actually learned the new value. NUMERIC(9,6)
            # is the storage scale, so compare at that scale — a sub-scale difference is not a
            # storable revision.
            new_rows.append((eff, rate, fetched_at))
            revised += 1

    if not new_rows:
        logger.info("rates: %d observations fetched, all already stored — nothing to insert", len(observations))
        return EXIT_OK

    # Provenance row and rate rows in ONE transaction: an interrupt must not leave a data_sources
    # row claiming rows that never landed (loaders review S-4 — both bar loaders already do this).
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, period_start, period_end, "
            "source_uri, row_count, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            ("fred", "risk_free_rates", fetched_at, observations[0][0], observations[-1][0],
             FRED_URL, len(new_rows),
             (f"FRED {FRED_SERIES} 3-month constant maturity, investment basis. "
              f"{len(observations)} observations fetched; {len(new_rows)} new rows "
              f"({revised} revisions). known_at: effective_date+1d floor for first observations, "
              "fetch time for revisions.")),
        )
        source_id = cur.fetchone()[0]
        cur.executemany(
            "INSERT INTO risk_free_rates (series, effective_date, annual_rate, known_at, source_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (series, effective_date, known_at) DO NOTHING",
            [(FRED_SERIES, e, r, k, source_id) for e, r, k in new_rows],
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(effective_date), max(effective_date), "
            "round(min(annual_rate)*100,3), round(max(annual_rate)*100,3) "
            "FROM risk_free_rates WHERE series=%s",
            (FRED_SERIES,),
        )
        n, lo, hi, rmin, rmax = cur.fetchone()
    logger.info(
        "rates: %s stored observations %s → %s, %s%% to %s%% "
        "(%d fetched, %d inserted of which %d revisions, %d non-publication days skipped)",
        f"{n:,}", lo, hi, rmin, rmax, len(observations), len(new_rows), revised, skipped)
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
    # A ROLLING horizon, not a fixed date. This defaulted to "2026-12-31", which was ~2 years out
    # when it was written and is now weeks away. The marking job refuses any date market_calendar
    # does not know, so the equity curve would have stopped dead on 1 January with an error naming
    # the calendar — and nothing would have said so in advance.
    #
    # Safe to generate years ahead: the holiday set is computed from NYSE rules (fixed dates with
    # weekend observance, nth-weekday rules, and a computed Good Friday), not fetched. Unforeseeable
    # closures — a national day of mourning — arrive through AD_HOC_CLOSURES and are picked up on
    # the next run, because the upsert updates in place.
    p.add_argument(
        "--to", dest="date_to",
        default=f"{date.today().year + 3}-12-31",
        help="default: 31 December, three years out (rolling)",
    )
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
    except FetchError as exc:
        # Before LoadError (its parent): a network failure is a connection problem, exit 3 —
        # reporting it as a validation failure (1) breaks the exit-code contract.
        logger.error("%s", exc)
        return EXIT_CONNECTION
    except LoadError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.OperationalError as exc:
        logger.error("connection lost: %s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("database error: %s", exc)
        return EXIT_SQL
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
