"""Mark every paper portfolio daily — the job that writes portfolio_returns_daily (issue #36).

WHY THIS JOB EXISTS
    portfolio_returns_daily is the observation table every Sharpe, Sortino, drawdown, and
    leaderboard row is computed from (EVALUATION_FRAMEWORK §2, §3.3), and until this job nothing
    wrote to it. Each run values portfolios against real prices, one row per (portfolio, trading
    session): market value, daily return, cumulative return, provenance.

THE MARKING FORMULA — load-bearing, and already wrong once (fix-pass B-S3)
    market_value(d) = Σ over open lots of  shares × split_factor_between(security_id, entry_date, d)
                                                  × RAW close(security_id, d)
                    + cash_at(d)

    RAW close, never adj_close. The catalog comment on price_bars_daily.adj_close ends "NEVER a
    marking price" for a measured reason: adj_close is on the CURRENT share basis while lot share
    counts are as-traded, so Σ shares × adj_close mis-marks any lot held across a split by the
    split factor — 40x for a pre-2021 NVDA lot (docs/fixpass/FIX_REPORT_phaseA.md B-S3).

    The catalog's SPLIT RULE (COMMENT ON paper_portfolio_positions) says the marking job multiplies
    shares by split_ratio and divides entry_price on each split ex-date. This module applies
    exactly that arithmetic, but AT VALUATION TIME via split_factor_between(entry_date, d) — the
    product of split ratios with ex_date in (entry_date, d] — rather than by mutating the lot rows.
    The two are the same number by construction (the factor IS the accumulated ex-date
    multiplication), and the read-time form is chosen deliberately:
      * the same catalog comment declares "Entries are immutable to the runtime role", and the
        grants agree — rh_app has no UPDATE on shares/entry_price, so a mutating design would
        need the migration role for routine marking;
      * an immutable ledger plus a deterministic factor is idempotent by construction — there is
        no "was this lot already adjusted for this split?" state to corrupt on a re-run.
    A split with ex_date ≤ entry_date is correctly a no-op (the lot was bought on the post-split
    basis; the factor's interval is exclusive at entry_date).

CASH — reconstructed from the lot ledger, not trusted as a constant
    paper_portfolios.cash is the CURRENT balance (a scalar; there is no cash-flow ledger table
    yet). A historical mark needs the balance as of that day, so it is reconstructed by walking
    the lot ledger backwards from today:

        cash_at(d) = cash_now
                   + Σ cost of lots entered AFTER d            (shares × entry_price)
                   - Σ proceeds of lots exited AFTER d         (shares × factor(entry→exit) × exit_price)

    This assumes lot entries and exits are the only cash flows — true today: dividend coverage is
    ~5% of securities and NOTHING credits dividends to cash yet, which is also why every return
    this job produces is PRICE-ONLY. When a dividend-crediting job lands, this reconstruction is
    no longer sound and a dated cash ledger must replace it; until then the data_sources note on
    every run states basis=price_only so evaluation_runs.return_basis can never honestly say
    'total_return' over these marks. A negative reconstructed balance means the ledger and the
    cash scalar disagree — that portfolio is refused loudly, never marked wrong quietly.

RETURNS
    daily_return(d)      = market_value(d) / market_value(prev session) - 1, computed from the
                           STORED (cent-quantized) values so a hand recompute from the table
                           reproduces it exactly. NULL when the previous session has no mark
                           (first mark, or the far side of a coverage hole — a gap-spanning
                           return would be a multi-session move wearing daily clothes, the exact
                           thing 007's coverage_ratio machinery exists to expose) or when the
                           previous value was zero (catalog rule on the column).
    cumulative_return(d) = market_value(d) / base_value - 1. Recomputable from the row itself
                           plus the portfolio row; no chained state to drift.

LIVE vs BACKFILL — honestly labelled (migration 009)
    mode=live      writes mark_kind='live' for a single session (the latest trading session on or
                   before today, or --date). ck_prd_mark_window holds a live mark to
                   [trade_date, trade_date+4d); this module refuses a live mark older than
                   LIVE_MAX_AGE_DAYS before even reaching the constraint, with the remedy named:
                   that is what backfill mode is for.
    mode=backfill  writes mark_kind='backfill' over an explicit --from/--to window. Any age is
                   legal because the row SAYS it was computed after the fact — the label is the
                   honesty mechanism a leakage audit reads (009's design).

IDEMPOTENCY — re-running must not double-write or drift
    portfolio_returns_daily is APPEND-ONLY (enforced by grants) and marks are scored history, so
    inserts use ON CONFLICT (portfolio_id, trade_date) DO NOTHING: an existing mark is never
    touched, and re-running a window writes only the holes. Return chaining reads existing marks
    from the table, so a resumed run continues the same series the first run started. When this
    run's recomputation disagrees with a stored mark by more than DRIFT_TOLERANCE (price data
    changed under the marks), the drift is REPORTED and the run exits non-zero — never repaired
    in place; corrections to scored history are the migration role's job.

COVERAGE — a data gap must be loud, not hidden
    A session where any open lot has no bar is SKIPPED and reported (portfolio, date, symbols),
    and the run exits non-zero. Carrying a stale price forward would hide the gap inside every
    downstream metric; skipping leaves a hole that market_calendar makes queryable and that
    evaluation_runs' expected_sessions/coverage_ratio (007) will surface on any metric row
    computed over it. Marking resumes cleanly after the gap is repaired (see IDEMPOTENCY).

PROVENANCE
    Each run inserts a data_sources row (provider='marking_job') recording mode, window, and
    basis=price_only; every mark written carries that source_id, and priced_as_of records when
    the prices were read. row_count is updated at the end of the run.

Usage (via bin/db_mark.sh, which supplies DATABASE_URL inside the internal network):
    python db/mark_portfolios.py live [--date 2026-08-12] [--portfolio ID ...] [--dry-run]
    python db/mark_portfolios.py backfill --from 2024-05-01 --to 2024-06-28 \
        [--portfolio ID ...] [--dry-run]

Exit codes match the other db/ loaders: 0 ok · 1 validation (including coverage holes and drift —
the marks that could be written honestly were) · 2 SQL failure · 3 connection failure.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - clear message beats a traceback
    print("mark_portfolios: psycopg (v3) is required. pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("mark_portfolios")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

PROVIDER = "marking_job"
DATASET = "portfolio_marks"
# What the returns ARE. Stated on every run's provenance row so the metric job cannot honestly
# store these marks under return_basis='total_return' (007/B-S2): nothing credits dividends yet.
RETURN_BASIS = "price_only"

# ck_prd_mark_window allows a live mark's priced_as_of in [trade_date, trade_date + 4d) UTC.
# Refuse at 3 days so the refusal is this module's readable message, not a constraint error —
# and so the insert cannot race the boundary mid-run.
LIVE_MAX_AGE_DAYS = 3

CENTS = Decimal("0.01")             # market_value is NUMERIC(18,2)
RETURN_QUANTUM = Decimal("1E-8")    # daily/cumulative returns are 8-decimal columns
DRIFT_TOLERANCE = Decimal("0.01")   # one cent: anything beyond quantization noise is real drift


@dataclass(frozen=True)
class Portfolio:
    id: int
    kind: str
    inception_date: date
    base_value: Decimal
    cash: Decimal
    closed_date: date | None  # (closed_at AT TIME ZONE 'UTC')::date, or None while open


@dataclass(frozen=True)
class Valuation:
    """One (portfolio, session) valuation before chaining."""
    trade_date: date
    equity: Decimal          # Σ effective shares × raw close, over lots that HAVE a bar
    open_lots: int
    priced_lots: int
    missing_symbols: list[str]
    entries_after: Decimal   # Σ cost of lots entered after this date (cash reconstruction)
    exits_after: Decimal     # Σ proceeds of lots exited after this date


@dataclass
class PortfolioOutcome:
    written: int = 0
    existing: int = 0
    holes: list[tuple[date, list[str]]] = field(default_factory=list)
    drift: list[tuple[date, Decimal, Decimal]] = field(default_factory=list)  # (date, stored, computed)
    error: str | None = None


# The valuation query. RAW close on purpose — adj_close is on the current share basis and is
# "NEVER a marking price" (catalog, B-S3); the share-basis alignment comes from
# split_factor_between(entry_date, session), which applies every split ex-date in the holding
# period exactly as the catalog's SPLIT RULE prescribes.
VALUATION_SQL = """
WITH sessions AS (
    SELECT unnest(%(dates)s::date[]) AS trade_date
),
lots AS (
    SELECT security_id, entry_date, exit_date, shares, entry_price, exit_price
    FROM paper_portfolio_positions
    WHERE portfolio_id = %(pid)s
),
open_lots AS (
    -- A lot counts on a session if entered on or before it and not yet exited. A lot exited ON
    -- session d does not count at d's close — its proceeds are already in cash_at(d) (the
    -- reconstruction treats exit_date <= d as settled), so counting it too would double it.
    SELECT s.trade_date,
           l.security_id,
           l.shares * split_factor_between(l.security_id, l.entry_date, s.trade_date)
               AS effective_shares
    FROM sessions s
    JOIN lots l
      ON l.entry_date <= s.trade_date
     AND (l.exit_date IS NULL OR l.exit_date > s.trade_date)
),
equity AS (
    -- LEFT JOIN so a missing bar is COUNTED (priced_lots < open_lots) rather than silently
    -- shrinking the sum; the caller skips such sessions loudly.
    SELECT o.trade_date,
           COALESCE(sum(o.effective_shares * b.close), 0) AS equity_value,
           count(*)::int                                  AS open_lots,
           count(b.close)::int                            AS priced_lots,
           array_remove(array_agg(CASE WHEN b.close IS NULL THEN sec.symbol END), NULL)
               AS missing_symbols
    FROM open_lots o
    JOIN securities sec ON sec.id = o.security_id
    LEFT JOIN price_bars_daily b
      ON b.security_id = o.security_id AND b.trade_date = o.trade_date
    GROUP BY o.trade_date
),
cash_adjustment AS (
    -- cash_at(d) = cash_now + entries_after(d) - exits_after(d): walking the ledger backwards
    -- from the current balance. Exit proceeds use the shares actually held at exit — the entry
    -- count carried through every split between entry and exit.
    SELECT s.trade_date,
           COALESCE(sum(l.shares * l.entry_price)
                        FILTER (WHERE l.entry_date > s.trade_date), 0) AS entries_after,
           COALESCE(sum(l.shares
                        * split_factor_between(l.security_id, l.entry_date, l.exit_date)
                        * l.exit_price)
                        FILTER (WHERE l.exit_date IS NOT NULL AND l.exit_date > s.trade_date), 0)
               AS exits_after
    FROM sessions s
    LEFT JOIN lots l ON TRUE
    GROUP BY s.trade_date
)
SELECT s.trade_date,
       COALESCE(e.equity_value, 0)     AS equity_value,
       COALESCE(e.open_lots, 0)        AS open_lots,
       COALESCE(e.priced_lots, 0)      AS priced_lots,
       COALESCE(e.missing_symbols, '{}') AS missing_symbols,
       c.entries_after,
       c.exits_after
FROM sessions s
LEFT JOIN equity e USING (trade_date)
JOIN cash_adjustment c USING (trade_date)
ORDER BY s.trade_date
"""

INSERT_SQL = """
INSERT INTO portfolio_returns_daily
    (portfolio_id, trade_date, market_value, daily_return, cumulative_return,
     priced_as_of, source_id, mark_kind)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (portfolio_id, trade_date) DO NOTHING
"""


def connect_from_env() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LookupError("DATABASE_URL is not set (run via bin/db_mark.sh, which assembles it)")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-mark")
    return conn


def trading_sessions(conn: psycopg.Connection, start: date, end: date) -> list[date]:
    rows = conn.execute(
        "SELECT trade_date FROM market_calendar "
        "WHERE is_trading_day AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def prior_session(conn: psycopg.Connection, d: date) -> date | None:
    row = conn.execute(
        "SELECT max(trade_date) FROM market_calendar WHERE is_trading_day AND trade_date < %s",
        (d,),
    ).fetchone()
    return row[0]


def latest_session_on_or_before(conn: psycopg.Connection, d: date) -> date | None:
    row = conn.execute(
        "SELECT max(trade_date) FROM market_calendar WHERE is_trading_day AND trade_date <= %s",
        (d,),
    ).fetchone()
    return row[0]


def load_portfolios(conn: psycopg.Connection, only_ids: list[int] | None) -> list[Portfolio]:
    sql = (
        "SELECT id, kind, inception_date, base_value, cash, "
        "       (closed_at AT TIME ZONE 'UTC')::date "
        "FROM paper_portfolios"
    )
    params: tuple = ()
    if only_ids:
        sql += " WHERE id = ANY(%s)"
        params = (only_ids,)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    portfolios = [Portfolio(*r) for r in rows]
    if only_ids:
        missing = set(only_ids) - {p.id for p in portfolios}
        if missing:
            raise ValueError(f"--portfolio id(s) not found: {sorted(missing)}")
    return portfolios


def value_sessions(conn: psycopg.Connection, pid: int, sessions: list[date]) -> dict[date, Valuation]:
    rows = conn.execute(VALUATION_SQL, {"pid": pid, "dates": sessions}).fetchall()
    return {
        r[0]: Valuation(
            trade_date=r[0], equity=r[1], open_lots=r[2], priced_lots=r[3],
            missing_symbols=list(r[4]), entries_after=r[5], exits_after=r[6],
        )
        for r in rows
    }


def existing_marks(conn: psycopg.Connection, pid: int, start: date, end: date) -> dict[date, Decimal]:
    rows = conn.execute(
        "SELECT trade_date, market_value FROM portfolio_returns_daily "
        "WHERE portfolio_id = %s AND trade_date BETWEEN %s AND %s",
        (pid, start, end),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def mark_portfolio(
    conn: psycopg.Connection,
    pf: Portfolio,
    sessions: list[date],
    *,
    mark_kind: str,
    priced_as_of: datetime,
    source_id: int | None,
    dry_run: bool,
) -> PortfolioOutcome:
    """Value one portfolio over the given sessions and insert the missing marks.

    The session list is clamped to [inception, closure] here, so callers pass the raw window.
    Chaining reads stored marks (including ones written moments ago by this run), so the series
    a resumed run produces is the series a single run would have produced.
    """
    out = PortfolioOutcome()

    window = [
        d for d in sessions
        if d >= pf.inception_date and (pf.closed_date is None or d <= pf.closed_date)
    ]
    if not window:
        return out

    valuations = value_sessions(conn, pf.id, window)
    prior = prior_session(conn, window[0])
    chain_start = prior if prior is not None else window[0]
    stored = existing_marks(conn, pf.id, chain_start, window[-1])

    prev_mv: Decimal | None = stored.get(prior) if prior is not None else None
    rows: list[tuple] = []

    for d in window:
        v = valuations[d]

        cash_at = pf.cash + v.entries_after - v.exits_after
        if cash_at < 0:
            # The ledger implies more money left the book than it ever held: the cash scalar and
            # the lot ledger disagree (an unrecorded flow, or a hand-edited balance). Marking on
            # top of that would be confidently wrong, so this portfolio is refused whole.
            out.error = (
                f"portfolio {pf.id}: reconstructed cash at {d} is {cash_at} (< 0) — the lot "
                f"ledger and paper_portfolios.cash disagree; fix the ledger before marking"
            )
            return out

        can_price = v.priced_lots == v.open_lots
        computed = (v.equity + cash_at).quantize(CENTS, ROUND_HALF_EVEN) if can_price else None

        if d in stored:
            # Idempotency: the stored mark is scored history and stays exactly as written. We
            # still recompute when possible, because a silent divergence between the table and
            # the data that should reproduce it is a data-integrity signal worth failing on.
            if computed is not None and abs(computed - stored[d]) > DRIFT_TOLERANCE:
                out.drift.append((d, stored[d], computed))
            prev_mv = stored[d]
            out.existing += 1
            continue

        if not can_price:
            out.holes.append((d, v.missing_symbols))
            # The far side of a hole must not disguise a multi-session move as a daily return.
            prev_mv = None
            continue

        daily = None
        if prev_mv is not None and prev_mv > 0:
            daily = (computed / prev_mv - 1).quantize(RETURN_QUANTUM, ROUND_HALF_EVEN)
        cumulative = (computed / pf.base_value - 1).quantize(RETURN_QUANTUM, ROUND_HALF_EVEN)

        rows.append((pf.id, d, computed, daily, cumulative, priced_as_of, source_id, mark_kind))
        prev_mv = computed

    if dry_run:
        for r in rows:
            logger.info("dry-run: would mark portfolio %s %s value=%s daily=%s cum=%s kind=%s",
                        r[0], r[1], r[2], r[3], r[4], r[7])
        out.written = len(rows)
        return out

    # One transaction per portfolio: a failure mid-book leaves no partial day-series behind, and
    # the idempotent re-run picks up exactly where the last committed portfolio ended.
    with conn.transaction():
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(INSERT_SQL, r)
                # rowcount 0 = a concurrent writer got there first; not double-written either way.
                out.written += cur.rowcount
    return out


def register_run(conn: psycopg.Connection, *, mode: str, start: date, end: date) -> int:
    row = conn.execute(
        "INSERT INTO data_sources (provider, dataset, fetched_at, period_start, period_end, notes) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            PROVIDER, DATASET, datetime.now(timezone.utc), start, end,
            (
                f"Portfolio marking run: mode={mode}; basis={RETURN_BASIS} (no dividends "
                f"credited — evaluation_runs.return_basis over these marks must say "
                f"'{RETURN_BASIS}'); formula=sum(as-traded shares x split factor x RAW close)"
                f"+ledger-reconstructed cash"
            ),
        ),
    ).fetchone()
    return row[0]


def finish_run(conn: psycopg.Connection, source_id: int, row_count: int) -> None:
    conn.execute("UPDATE data_sources SET row_count = %s WHERE id = %s", (row_count, source_id))


def parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {raw!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mark_portfolios",
        description="Value paper portfolios daily into portfolio_returns_daily (issue #36).",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--portfolio", type=int, action="append", metavar="ID",
                        help="restrict to these portfolio ids (repeatable; default: all)")
    common.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")

    sub = parser.add_subparsers(dest="mode", required=True)
    live = sub.add_parser("live", parents=[common],
                          help="mark one recent session, labelled mark_kind='live'")
    live.add_argument("--date", type=parse_iso_date, default=None,
                      help="session to mark (default: latest trading session on or before today)")
    back = sub.add_parser("backfill", parents=[common],
                          help="mark a historical window, labelled mark_kind='backfill'")
    back.add_argument("--from", dest="from_date", type=parse_iso_date, required=True,
                      help="window start (inclusive)")
    back.add_argument("--to", dest="to_date", type=parse_iso_date, required=True,
                      help="window end (inclusive)")
    return parser


def run(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    today = datetime.now(timezone.utc).date()

    if args.mode == "live":
        target = args.date or latest_session_on_or_before(conn, today)
        if target is None:
            logger.error("market_calendar has no trading session on or before %s — load the calendar", today)
            return EXIT_VALIDATION
        if (today - target).days > LIVE_MAX_AGE_DAYS:
            logger.error(
                "refusing a LIVE mark for %s: %d days old exceeds the honest live window "
                "(%d days; ck_prd_mark_window). A historical mark must say so — use: "
                "backfill --from %s --to %s",
                target, (today - target).days, LIVE_MAX_AGE_DAYS, target, target,
            )
            return EXIT_VALIDATION
        sessions = trading_sessions(conn, target, target)
        if not sessions:
            logger.error("%s is not a trading session in market_calendar", target)
            return EXIT_VALIDATION
        window_start = window_end = target
        mark_kind = "live"
    else:
        if args.from_date > args.to_date:
            logger.error("--from %s is after --to %s", args.from_date, args.to_date)
            return EXIT_VALIDATION
        sessions = trading_sessions(conn, args.from_date, args.to_date)
        if not sessions:
            logger.error("market_calendar has no trading sessions in [%s, %s] — load the calendar "
                         "before backfilling", args.from_date, args.to_date)
            return EXIT_VALIDATION
        window_start, window_end = args.from_date, args.to_date
        mark_kind = "backfill"

    portfolios = load_portfolios(conn, args.portfolio)
    if not portfolios:
        logger.info("no paper portfolios exist — nothing to mark")
        return EXIT_OK

    source_id: int | None = None
    if not args.dry_run:
        source_id = register_run(conn, mode=args.mode, start=window_start, end=window_end)

    priced_as_of = datetime.now(timezone.utc)
    total_written = 0
    had_validation_failure = False

    for pf in portfolios:
        out = mark_portfolio(
            conn, pf, sessions,
            mark_kind=mark_kind, priced_as_of=priced_as_of,
            source_id=source_id, dry_run=args.dry_run,
        )
        total_written += out.written

        if out.error:
            logger.error("%s", out.error)
            had_validation_failure = True
            continue
        for d, symbols in out.holes:
            logger.error("portfolio %s: no bar for %s on %s — session SKIPPED (repair the bars, "
                         "then re-run; the hole fills idempotently)", pf.id, ", ".join(symbols), d)
        for d, stored_mv, computed_mv in out.drift:
            logger.error("portfolio %s: stored mark %s = %s but recomputation says %s — price data "
                         "changed under scored history; correction is the migration role's job",
                         pf.id, d, stored_mv, computed_mv)
        if out.holes or out.drift:
            had_validation_failure = True

        expected = len([
            d for d in sessions
            if d >= pf.inception_date and (pf.closed_date is None or d <= pf.closed_date)
        ])
        logger.info("portfolio %s (%s): sessions=%d written=%d existing=%d holes=%d drift=%d",
                    pf.id, pf.kind, expected, out.written, out.existing,
                    len(out.holes), len(out.drift))

    if source_id is not None:
        finish_run(conn, source_id, total_written)

    logger.info("%s%s complete: %d mark(s) written across %d portfolio(s), window [%s, %s]",
                "dry-run " if args.dry_run else "", args.mode, total_written,
                len(portfolios), window_start, window_end)
    return EXIT_VALIDATION if had_validation_failure else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)

    try:
        conn = connect_from_env()
    except LookupError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.OperationalError as exc:
        logger.error("could not connect: %s", exc)
        return EXIT_CONNECTION

    try:
        return run(conn, args)
    except ValueError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.OperationalError as exc:
        logger.error("connection lost: %s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("SQL failure: %s", exc)
        return EXIT_SQL
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
