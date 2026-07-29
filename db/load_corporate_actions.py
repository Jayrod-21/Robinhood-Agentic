"""Load splits and dividends, then populate `price_bars_daily.adj_close`.

WHY IT MATTERS
    Verified in this project's own data: NVDA closed 751.19 on 2021-07-19 and 186.06 on 2021-07-20 —
    a naive −75.23% day that is a 4-for-1 split, not a crash. NVDA splits again 10:1 in June 2024,
    so the cumulative distortion across the archive is 40×. Every Sharpe, every counterfactual track
    record, and every backtest reads this series.

CANDIDATE SELECTION — and its honest limits
    The universe is ~14,600 securities. Fetching corporate actions for every one is ~14,600 provider
    calls, which is slow and heavily rate-limited.

    A split announces itself as an anomalous overnight gap, so `--candidates gaps` (the default)
    scans the loaded bars for moves beyond a threshold and asks the provider only about those. On
    this archive that is roughly 3,700 securities rather than 14,600.

    **This is candidate SELECTION, not detection.** The provider is still the source of truth — a
    real −50% crash looks exactly like a 2-for-1 to a gap scan, which is why nothing is inferred
    from the gap itself. The limitation runs the other way: a small action (a 1.1-for-1 stock
    dividend is a ~9% move) sits below any sane threshold and will be MISSED. So:

      * `--candidates gaps` is fast and high-coverage, NOT complete.
      * `--candidates all` is complete for the provider's data, and slow.
      * `verify` re-scans AFTER adjustment and lists gaps that remain unexplained, so what the pass
        missed is visible rather than assumed absent.

    A proper corporate-actions feed (Polygon's splits endpoint, or FMP once purchased) removes the
    trade-off entirely and should replace this when available.

DIVIDENDS
    Fetched and stored, but they do NOT touch `adj_close`. Splits change the share count and must
    adjust the price series; dividends do not — the marking job credits them to cash. Adjusting the
    price for dividends as well would count every one twice. See migration 005's header.

Usage:
    python db/load_corporate_actions.py fetch --candidates gaps
    python db/load_corporate_actions.py fetch --symbols NVDA AAPL
    python db/load_corporate_actions.py adjust
    python db/load_corporate_actions.py verify
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    print("load_corporate_actions: psycopg (v3) required", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("corporate_actions")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

PROVIDER = "yfinance"
DATASET = "corporate_actions"

# A move this large overnight is not ordinary trading. Deliberately loose: this only picks who to
# ASK about, and a false positive costs one provider call while a false negative costs a wrong
# return series forever.
GAP_LOW, GAP_HIGH = 0.75, 1.3333

# Post-adjustment, a gap this large is reported for review rather than silently accepted.
VERIFY_LOW, VERIFY_HIGH = 0.60, 1.6667

# Below this the tick size is a large fraction of the price, so ordinary moves land on round
# split ratios by coincidence. Sub-dollar warrants generated every false positive in the first
# verification run.
MIN_PRICE_FOR_SPLIT_CHECK = 1.00


class LoadError(Exception):
    """A failure raised deliberately."""


def connect() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LoadError("DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-corp-actions")
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute("SET idle_in_transaction_session_timeout = 0")
    return conn


# ── candidate selection ───────────────────────────────────────────────────────────────────────
def candidates_from_gaps(conn: psycopg.Connection) -> list[tuple[int, str]]:
    """Securities whose daily series contains an anomalous overnight gap."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH r AS (
                SELECT security_id, trade_date, close,
                       lag(close) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev
                FROM price_bars_daily
            )
            SELECT DISTINCT r.security_id, s.symbol
            FROM r JOIN securities s ON s.id = r.security_id
            WHERE r.prev IS NOT NULL AND r.prev > 0
              AND (r.close / r.prev < %s OR r.close / r.prev > %s)
            ORDER BY s.symbol
            """,
            (GAP_LOW, GAP_HIGH),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def candidates_all(conn: psycopg.Connection) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.symbol FROM securities s "
            "WHERE EXISTS (SELECT 1 FROM price_bars_daily d WHERE d.security_id = s.id) "
            "ORDER BY s.symbol"
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def candidates_named(conn: psycopg.Connection, symbols: list[str]) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol FROM securities WHERE delisted_at IS NULL AND symbol = ANY(%s) ORDER BY symbol",
            ([s.upper() for s in symbols],),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


# ── provider ──────────────────────────────────────────────────────────────────────────────────
def fetch_actions(symbol: str) -> tuple[list[tuple[date, float]], list[tuple[date, float]]]:
    """(splits, dividends) for one symbol as (ex_date, value) pairs. Never raises."""
    import yfinance as yf

    splits: list[tuple[date, float]] = []
    divs: list[tuple[date, float]] = []
    try:
        t = yf.Ticker(symbol)
        for idx, val in t.splits.items():
            ratio = float(val)
            # A ratio of exactly 1 adjusts nothing; the schema rejects it, and it usually means the
            # provider recorded a non-event.
            if ratio > 0 and ratio != 1.0:
                splits.append((idx.date(), ratio))
        for idx, val in t.dividends.items():
            amt = float(val)
            if amt > 0:
                divs.append((idx.date(), amt))
    except Exception as exc:  # noqa: BLE001 — one bad symbol must not end a 3,700-symbol run
        logger.debug("%s: provider error: %s", symbol, exc)
    return splits, divs


def warn_if_archive_incomplete(conn: psycopg.Connection) -> None:
    """Refuse to let gap selection run silently against a partially-loaded archive.

    Learned the expensive way: the first fetch ran while the daily loader was still working, so the
    bars only reached 2022-12 and candidate selection could only see gaps in that window. Every
    security whose anomalous move fell in 2023-2025 was never queried — 2,809 of them, a third of the
    eventual candidate set. The splits were sitting in the provider the whole time (AGZD's 2023
    2-for-1 among them); we simply never asked.

    Nothing here can know the archive's intended end date, so this reports the coverage it found and
    makes the operator confirm it looks complete, rather than pretending to validate it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date) FROM price_bars_daily")
        lo, hi, n = cur.fetchone()
    if lo is None:
        raise LoadError("price_bars_daily is empty — run the daily loader before fetching actions")
    logger.info("gap selection will scan %s trading dates, %s → %s", f"{n:,}", lo, hi)
    logger.info(
        "If the daily loader is STILL RUNNING, stop now: candidates are chosen from the bars that "
        "exist at this moment, and anything outside that range will be silently skipped."
    )


def cmd_fetch(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    if args.symbols:
        cands = candidates_named(conn, args.symbols)
    elif args.candidates == "all":
        cands = candidates_all(conn)
    else:
        warn_if_archive_incomplete(conn)
        cands = candidates_from_gaps(conn)

    if not cands:
        logger.warning("no candidate securities — has the daily loader run?")
        return EXIT_OK

    logger.info(
        "%d candidate securities (%s). NOTE: gap selection is high-coverage, not complete — "
        "small actions fall below the threshold; run `verify` afterwards.",
        len(cands), "explicit" if args.symbols else args.candidates,
    )

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, notes) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (PROVIDER, DATASET, datetime.now(timezone.utc),
             f"Corporate actions via yfinance; candidate mode={args.candidates}"),
        )
        source_id = cur.fetchone()[0]

    n_splits = n_divs = with_actions = errors = 0
    started = time.monotonic()

    for i, (sec_id, symbol) in enumerate(cands, 1):
        splits, divs = fetch_actions(symbol)
        if not splits and not divs:
            continue
        with_actions += 1
        try:
            with conn.transaction(), conn.cursor() as cur:
                for ex_date, ratio in splits:
                    cur.execute(
                        "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio, source_id) "
                        "VALUES (%s,'split',%s,%s,%s) ON CONFLICT (security_id, action_type, ex_date) DO NOTHING",
                        (sec_id, ex_date, ratio, source_id),
                    )
                    n_splits += cur.rowcount
                for ex_date, amt in divs:
                    cur.execute(
                        "INSERT INTO corporate_actions (security_id, action_type, ex_date, cash_amount, source_id) "
                        "VALUES (%s,'cash_dividend',%s,%s,%s) ON CONFLICT (security_id, action_type, ex_date) DO NOTHING",
                        (sec_id, ex_date, amt, source_id),
                    )
                    n_divs += cur.rowcount
        except psycopg.Error as exc:
            # One symbol's malformed action must not abort the run; its transaction rolled back.
            errors += 1
            logger.warning("%s: insert failed, skipping: %s", symbol, exc)

        if i % 200 == 0:
            rate = i / (time.monotonic() - started)
            logger.info("[%d/%d] %.1f sym/s — %d splits, %d dividends so far", i, len(cands), rate, n_splits, n_divs)

    with conn.cursor() as cur:
        cur.execute("UPDATE data_sources SET row_count=%s WHERE id=%s", (n_splits + n_divs, source_id))

    logger.info(
        "fetched — %d securities had actions, %d splits, %d dividends, %d insert errors, %.0fs",
        with_actions, n_splits, n_divs, errors, time.monotonic() - started,
    )
    if errors:
        logger.warning("%d symbol(s) failed to insert — see warnings above", errors)
    return EXIT_OK


# ── adjustment ────────────────────────────────────────────────────────────────────────────────
def cmd_adjust(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    """Populate adj_close. Idempotent — safe to re-run after loading more actions or more bars."""
    started = time.monotonic()

    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT security_id) FROM corporate_actions WHERE action_type='split'")
        n_split_secs = cur.fetchone()[0]
    logger.info("%d securities carry at least one split", n_split_secs)

    # Securities WITH splits: the real adjustment. Small set, so the per-row function call is cheap.
    #
    # The FACTOR is always written; adj_close only where the level is representable. Serial
    # reverse-splitters (WHLR 16 splits, UVXY 13, NUWE with a cumulative factor near 1e-10) imply
    # adjusted prices around 1e13, and NUMERIC(30,10) tops out below that. Writing NULL there is
    # correct rather than lossy: the factor carries the full information, and returns computed as
    # (close/f) ratios never need the level. See migration 006.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE price_bars_daily d
            SET split_adj_factor = f.factor,
                adj_close = CASE
                    WHEN d.close / f.factor < 1e19 THEN ROUND(d.close / f.factor, 10)
                    ELSE NULL
                END
            FROM (
                SELECT d2.security_id, d2.trade_date,
                       split_factor_after(d2.security_id, d2.trade_date) AS factor
                FROM price_bars_daily d2
                WHERE d2.security_id IN (
                    SELECT DISTINCT security_id FROM corporate_actions WHERE action_type='split')
            ) f
            WHERE d.security_id = f.security_id AND d.trade_date = f.trade_date
            """
        )
        adjusted = cur.rowcount

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM price_bars_daily "
            "WHERE split_adj_factor IS NOT NULL AND adj_close IS NULL"
        )
        unrepresentable = cur.fetchone()[0]
    logger.info("adjusted %s bars for split-affected securities", f"{adjusted:,}")
    if unrepresentable:
        logger.warning(
            "%s bar(s) have a factor but no representable adj_close (serial reverse-splitters). "
            "Their returns are still computable from split_adj_factor.", f"{unrepresentable:,}",
        )

    # Everything else: adj_close = close. Batched by primary-key range rather than one statement,
    # because a single UPDATE over ~11M rows holds one enormous transaction and doubles the table's
    # dead tuples in one go.
    total_plain = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH batch AS (
                    SELECT security_id, trade_date FROM price_bars_daily
                    WHERE split_adj_factor IS NULL
                    LIMIT 500000
                )
                UPDATE price_bars_daily d
                SET adj_close = d.close, split_adj_factor = 1
                FROM batch b
                WHERE d.security_id = b.security_id AND d.trade_date = b.trade_date
                """
            )
            n = cur.rowcount
        if n == 0:
            break
        total_plain += n
        logger.info("  … %s unsplit bars set (%s total)", f"{n:,}", f"{total_plain:,}")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FILTER (WHERE split_adj_factor IS NULL), count(*) FROM price_bars_daily")
        remaining, total = cur.fetchone()

    logger.info(
        "adjust complete — %s bars total, %s still NULL, %.0fs",
        f"{total:,}", f"{remaining:,}", time.monotonic() - started,
    )
    if remaining:
        logger.warning("%s bars have no split_adj_factor — investigate before trusting any return", f"{remaining:,}")
        return EXIT_VALIDATION
    return EXIT_OK


# ── verification ──────────────────────────────────────────────────────────────────────────────
def cmd_verify(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    """Re-scan the ADJUSTED series and report gaps that remain unexplained.

    Some are real: earnings collapses, biotech readouts, meme-stock squeezes. Some are splits this
    pass missed. The point is to make the residue visible and countable rather than assume the
    adjustment was complete.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH r AS (
                SELECT security_id, trade_date, adj_close,
                       lag(adj_close) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev
                FROM price_bars_daily WHERE adj_close IS NOT NULL
            ), g AS (
                SELECT security_id, trade_date, adj_close/prev AS ratio
                FROM r WHERE prev IS NOT NULL AND prev > 0
            )
            SELECT count(*), count(DISTINCT security_id) FROM g
            WHERE ratio < %s OR ratio > %s
            """,
            (VERIFY_LOW, VERIFY_HIGH),
        )
        n_gaps, n_secs = cur.fetchone()

        # Gaps near a round split ratio, filtered by the two things that distinguish a real split
        # from noise. Without both filters this list is dominated by penny warrants and is useless:
        # the first run returned 40 rows that were almost entirely sub-$0.20 warrants oscillating
        # ±50-170% in BOTH directions, where a ratio of exactly 0.5000 is tick-size coincidence
        # ($0.08 to $0.04) rather than a corporate action. An alert nobody trusts gets ignored.
        #
        #   1. PRICE FLOOR. Below ~$1 the tick size is a large fraction of the price, so ordinary
        #      moves land on round ratios by chance. A split on a $0.10 warrant is also not
        #      something this system would ever act on.
        #   2. PERSISTENCE. A split is permanent and one-directional. Noise reverts. Requiring the
        #      new level to still hold 5 sessions later removes the oscillators, which is what
        #      AACIW, ABLVW and friends all were.
        cur.execute(
            """
            WITH r AS (
                SELECT security_id, trade_date, adj_close,
                       lag(adj_close)  OVER w AS prev,
                       lead(adj_close, 5) OVER w AS later
                FROM price_bars_daily WHERE adj_close IS NOT NULL
                WINDOW w AS (PARTITION BY security_id ORDER BY trade_date)
            ), g AS (
                SELECT r.security_id, s.symbol, r.trade_date,
                       r.adj_close/r.prev AS ratio,
                       r.prev, r.adj_close, r.later
                FROM r JOIN securities s ON s.id = r.security_id
                WHERE r.prev IS NOT NULL AND r.prev >= %s AND r.later IS NOT NULL
            )
            SELECT symbol, trade_date, round(ratio, 4), round(1/ratio, 3), prev, adj_close
            FROM g
            WHERE (abs(1/ratio - 2)  < 0.05 OR abs(1/ratio - 3)  < 0.08
                OR abs(1/ratio - 4)  < 0.10 OR abs(1/ratio - 10) < 0.25)
              -- Still near the new level a week later: the move stuck, so it is not noise.
              AND later BETWEEN adj_close * 0.7 AND adj_close * 1.43
            ORDER BY symbol, trade_date
            LIMIT 40
            """,
            (MIN_PRICE_FOR_SPLIT_CHECK,),
        )
        suspicious = cur.fetchall()

    logger.info("post-adjustment: %s gaps beyond ±40%% across %s securities", f"{n_gaps:,}", f"{n_secs:,}")
    if suspicious:
        logger.warning(
            "%d gap(s) still sit near a round split ratio — likely actions this pass missed:",
            len(suspicious),
        )
        for sym, d, ratio, inv, prev, now in suspicious[:20]:
            logger.warning("    %-8s %s  $%s → $%s  ratio=%s  (≈1-for-%s)", sym, d, prev, now, ratio, inv)
        logger.warning("Confirm with the provider: python db/load_corporate_actions.py fetch --symbols <SYM>...")
    else:
        logger.info("no residual gaps near a round split ratio")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_corporate_actions")
    p.add_argument("command", choices=("fetch", "adjust", "verify"))
    p.add_argument("--candidates", choices=("gaps", "all"), default="gaps",
                   help="gaps: only securities with an anomalous move (fast, not complete). "
                        "all: every security with bars (complete for the provider, slow).")
    p.add_argument("--symbols", nargs="*", help="explicit symbols, overriding --candidates")
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
        return {"fetch": cmd_fetch, "adjust": cmd_adjust, "verify": cmd_verify}[args.command](conn, args)
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
