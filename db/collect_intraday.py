"""Collect a 30-minute intraday observation for every security this system reasons about.

ISSUE #133. One sweep = one row in intraday_collection_runs plus one intraday_observations row per
security in scope. Meant to run on a timer during regular trading hours.

WHAT IS IN SCOPE
    Positions held, names debated recently, and names in a recent pipeline proposal — union,
    with the reason(s) recorded on every row. The reason is not decoration: without it a security
    disappearing from the series is ambiguous between "the collector stopped" and "it left the
    watchlist", and those need different responses.

    Non-investable instruments are excluded (migration 025). There is no reason to log a warrant's
    P/E, and after #41 we can finally tell which rows those are.

MARKET HOURS COME FROM THE CALENDAR, NOT FROM A WEEKDAY CHECK
    market_calendar carries session_open and session_close per date, so half-days and holidays are
    right without a hardcoded 09:30-16:00. A sweep outside the session records status='skipped'
    with the reason, rather than writing an observation of a stale quote — a price that has not
    moved since Friday is not an observation, it is the same observation again.

FAILURE IS RECORDED, NOT SWALLOWED
    A quote that fails leaves NO observation row for that security and increments `failed` on the
    run. It never writes the last-good price forward. Same rule as everywhere else here: an absent
    measurement must not be able to impersonate a taken one.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Imported after the sys.path insert, same as the other loaders in this directory.
from instrument_class import INVESTABLE, provider_symbols
from intraday_ratios import FORMULA_VERSION, compute

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collect_intraday")

FMP_QUOTE = "https://financialmodelingprep.com/stable/quote"
TIMEOUT_SECONDS = 20

# How far back a debate or a proposal keeps a name in scope. Long enough that a name debated last
# week still accumulates history while the decision is live; short enough that the set does not grow
# without bound as the archive does.
SCOPE_LOOKBACK_DAYS = 30


class CollectError(RuntimeError):
    pass


def connect() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise CollectError("DATABASE_URL is not set")
    return psycopg.connect(dsn, autocommit=True, application_name="rh-intraday")


# ── scope ─────────────────────────────────────────────────────────────────────────────────────

_SCOPE_SQL = """
WITH held AS (
    SELECT DISTINCT p.security_id, 'held' AS reason
      FROM paper_portfolio_positions p
), debated AS (
    SELECT DISTINCT d.security_id, 'debated' AS reason
      FROM debates d
     WHERE d.security_id IS NOT NULL
       AND d.started_at > now() - make_interval(days => %(lookback)s)
), proposed AS (
    SELECT DISTINCT ap.security_id, 'proposed' AS reason
      FROM agent_proposals a
      JOIN agent_proposal_positions ap ON ap.proposal_id = a.id
     WHERE a.created_at > now() - make_interval(days => %(lookback)s)
)
SELECT s.id, s.symbol, array_agg(DISTINCT u.reason ORDER BY u.reason) AS reasons
  FROM (SELECT * FROM held UNION ALL SELECT * FROM debated UNION ALL SELECT * FROM proposed) u
  JOIN securities s ON s.id = u.security_id
 -- Migration 025. A warrant's P/E is not a thing worth 13 rows a day.
 WHERE s.security_type = ANY(%(investable)s)
 GROUP BY s.id, s.symbol
 ORDER BY s.symbol
"""


def scope(conn: psycopg.Connection, lookback_days: int = SCOPE_LOOKBACK_DAYS) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(_SCOPE_SQL, {"lookback": lookback_days, "investable": list(INVESTABLE)})
        return cur.fetchall()


def fundamentals_in_effect(conn: psycopg.Connection, security_id: int, at: datetime) -> dict | None:
    """The ONE statement row in effect at `at`, or None.

    Latest known_at not after the observation. Deliberately not a per-column forward fill across
    rows: fundamentals_id names this row, and a denominator taken from a different vintage would
    make that FK a lie — see the migration's comment.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, eps_current, eps_next_year_est, free_cash_flow"
            "  FROM fundamentals_snapshots"
            " WHERE security_id = %s AND known_at IS NOT NULL AND known_at <= %s"
            " ORDER BY known_at DESC LIMIT 1",
            (security_id, at),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "eps_current": row[1],
        "eps_next_year_est": row[2],
        "free_cash_flow": row[3],
    }


# ── session gating ────────────────────────────────────────────────────────────────────────────


def session_window(conn: psycopg.Connection, at: datetime) -> tuple[datetime, datetime] | None:
    """(open, close) for the trading day `at` falls on, or None when the market is closed.

    From market_calendar, so a half-day closes at 13:00 without anything here knowing that 13:00 is
    special. A date with no row is treated as closed — an unknown calendar is not an open market.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_open, session_close FROM market_calendar"
            " WHERE trade_date = (%s AT TIME ZONE 'America/New_York')::date AND is_trading_day",
            (at,),
        )
        row = cur.fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    return row[0], row[1]


# ── the quote ─────────────────────────────────────────────────────────────────────────────────


def fetch_quote(symbol: str, api_key: str) -> dict | None:
    """One quote, or None. Never raises — one bad symbol must not end the sweep.

    Tries every provider spelling. Our archive writes share classes with a dot (BRK.B); FMP's quote
    endpoint answers 402 Payment Required for that and 200 for BRK-B — a status that reads like a
    plan limit rather than a symbol it does not recognise, which is why this took a live run to
    find. BRK.B is a held position, so it was a real hole in the series, not a curiosity.
    """
    for candidate in provider_symbols(symbol):
        url = f"{FMP_QUOTE}?{urllib.parse.urlencode({'symbol': candidate, 'apikey': api_key})}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.debug("quote failed for %s as %s: %s", symbol, candidate, exc)
            continue

        row = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(row, dict) and row.get("price") not in (None, 0):
            if candidate != symbol:
                logger.info("%s quoted under the provider spelling %s", symbol, candidate)
            return row

    # Every spelling exhausted. WARNING, not debug: this leaves a hole in the series for a security
    # that is in scope, and a hole nobody notices is the failure this whole table is built against.
    logger.warning("no usable quote for %s (tried %s)", symbol, ", ".join(provider_symbols(symbol)))
    return None


# ── the sweep ─────────────────────────────────────────────────────────────────────────────────


def collect(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        raise CollectError("FMP_API_KEY is not set")

    now = datetime.now(timezone.utc)
    window = session_window(conn, now)
    session_date = now.date()

    if window is None or not (window[0] <= now <= window[1]):
        reason = (
            "market closed (no trading session on this date)"
            if window is None
            else f"outside the session window {window[0]:%H:%M}-{window[1]:%H:%M} UTC"
        )
        logger.info("skipping: %s", reason)
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO intraday_collection_runs"
                    " (session_date, status, completed_at, error) VALUES (%s,'skipped',now(),%s)",
                    (session_date, reason),
                )
        return 0

    targets = scope(conn, args.lookback_days)
    logger.info("scope: %d security(ies)", len(targets))
    if args.dry_run:
        for _sid, symbol, reasons in targets:
            logger.info("  %-6s %s", symbol, ",".join(reasons))
        logger.info("DRY RUN — nothing written")
        return 0

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO intraday_collection_runs (session_date, scope_size) VALUES (%s,%s)"
            " RETURNING id",
            (session_date, len(targets)),
        )
        run_id = cur.fetchone()[0]

    observed = failed = 0
    for security_id, symbol, reasons in targets:
        quote = fetch_quote(symbol, api_key)
        if quote is None:
            # No row. NOT the last-good price written forward — an absent measurement must not be
            # able to impersonate a taken one.
            failed += 1
            continue

        fundamentals = fundamentals_in_effect(conn, security_id, now)
        ratios = compute(
            price=quote["price"], market_cap=quote.get("marketCap"), fundamentals=fundamentals
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO intraday_observations
                        (run_id, security_id, observed_at, session_date, scope_reasons,
                         price, market_cap, volume, pe_trailing, pe_forward, fcf_yield,
                         fundamentals_id, formula_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (security_id, observed_at) DO UPDATE SET
                        price = EXCLUDED.price, market_cap = EXCLUDED.market_cap,
                        volume = EXCLUDED.volume, pe_trailing = EXCLUDED.pe_trailing,
                        pe_forward = EXCLUDED.pe_forward, fcf_yield = EXCLUDED.fcf_yield,
                        fundamentals_id = EXCLUDED.fundamentals_id,
                        formula_version = EXCLUDED.formula_version
                    """,
                    (
                        run_id, security_id, now, session_date, list(reasons),
                        quote["price"], quote.get("marketCap"),
                        int(quote["volume"]) if quote.get("volume") is not None else None,
                        ratios["pe_trailing"], ratios["pe_forward"], ratios["fcf_yield"],
                        (fundamentals or {}).get("id"), FORMULA_VERSION,
                    ),
                )
            observed += 1
        except psycopg.Error as exc:
            logger.error("could not store %s: %s", symbol, exc)
            failed += 1

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE intraday_collection_runs SET status='complete', completed_at=now(),"
            " observed=%s, failed=%s WHERE id=%s",
            (observed, failed, run_id),
        )
    # Loud when anything failed. A sweep that quietly recorded 3 of 15 looks like a sweep.
    log = logger.warning if failed else logger.info
    log("run %d: %d observed, %d failed of %d in scope", run_id, observed, failed, len(targets))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve scope and report it; write nothing")
    parser.add_argument("--lookback-days", type=int, default=SCOPE_LOOKBACK_DAYS,
                        help="how long a debate or proposal keeps a name in scope")
    args = parser.parse_args(argv)

    try:
        conn = connect()
    except CollectError as exc:
        logger.error("%s", exc)
        return 3
    try:
        return collect(conn, args)
    except CollectError as exc:
        logger.error("%s", exc)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
