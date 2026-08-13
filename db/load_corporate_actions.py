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

    COVERAGE WARNING: gap selection finds SPLITS (large one-day moves). Dividends are small moves,
    so `--candidates gaps` structurally cannot find dividend payers that never split — measured
    coverage after the gaps run was 960 of 19,301 securities (5.0%), with SPY at zero. Until a
    `--candidates all` run (or a real corporate-actions feed) has loaded dividends for the
    universe, the marking job has nothing to credit and any computed return is PRICE-ONLY.
    `evaluation_runs.return_basis` (migration 007) exists so that a price-only number can never be
    stored as a total return.

POINT-IN-TIME BOUND (migration 007)
    The adjustment is bounded by an explicit `adjustment_as_of` date — the archive's last covered
    session, recorded in `price_adjustment_state`. Splits with ex_date AFTER that date are
    excluded: yfinance returns splits through TODAY, and folding a 2026 reverse split into a 2025
    price level manufactures lookahead (527 such splits contaminated 308,709 bars before this
    bound existed). Returns computed from the stored factor are point-in-time safe at every date —
    factor(t-1)/factor(t) only ever embeds splits with ex_date ≤ t. Price LEVELS are only safe for
    decisions at or after adjustment_as_of; a historical decision date needs
    `split_factor_between(security_id, bar_date, decision_date)`.

Usage:
    python db/load_corporate_actions.py fetch --candidates gaps
    python db/load_corporate_actions.py fetch --symbols NVDA AAPL
    python db/load_corporate_actions.py adjust
    python db/load_corporate_actions.py verify
"""

from __future__ import annotations

import argparse
import logging
import math
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
# return series forever. Known misses below the band: 3-for-2 (1.5) is caught, but 5-for-4 (1.25)
# and any stock dividend under ~33% (a 1.1-for-1 is a ~9% move) sit inside it and are NOT — that
# is the structural limit of gap selection, not a tunable.
GAP_LOW, GAP_HIGH = 0.75, 1.3333

# Post-adjustment, a gap this large is reported for review rather than silently accepted.
VERIFY_LOW, VERIFY_HIGH = 0.60, 1.6667

# Below this the tick size is a large fraction of the price, so ordinary moves land on round
# split ratios by coincidence. Sub-dollar warrants generated every false positive in the first
# verification run.
MIN_PRICE_FOR_SPLIT_CHECK = 1.00

# If this many candidates fail at the provider back-to-back with none succeeding, the problem is
# connectivity (wrong wrapper / no egress / hard rate-limit), not the symbols — abort in seconds
# instead of burning hours logging one warning per name.
CONSECUTIVE_PROVIDER_FAILURES_ABORT = 15

# adj_close is NULLed above this level. NOT a representability bound — NUMERIC(30,10) holds up to
# 1e20-ε — but a meaning bound: no real instrument has a $1e12 per-share price, and 006's contract
# is that a NULL level says "use the factor" while a stored absurd level says nothing. The previous
# guard (1e19) could never fire below column overflow, which made 006's documented fail-safe dead
# code (semantics review S-S9).
MAX_MEANINGFUL_ADJ_CLOSE = 1e12


class LoadError(Exception):
    """A failure raised deliberately."""


class ProviderError(LoadError):
    """The provider could not be asked — as distinct from 'the provider had no actions'.

    The distinction is the whole point (loaders review B-1): a swallowed provider failure reads as
    'no corporate actions', the run exits 0, and `adjust` then bakes split_adj_factor = 1 into the
    return series as a positive claim. Silent, persistent corruption reported as success.
    """


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
# All three selectors resolve to LIVE securities only (delisted_at IS NULL), for a reason beyond
# tidiness: the provider is queried BY SYMBOL, and yfinance can only speak for the symbol's
# CURRENT holder. Asking it about a delisted identity would attribute the re-listed issuer's
# actions to the dead one's price series — a wrong adjustment layered on the wrong company. The
# delisted cohort's bars therefore stay unadjusted unless a feed that knows historical identities
# (FMP, Polygon reference) supplies their actions; each selector counts and reports what it
# excluded so that limitation is visible, never silent.

def candidates_from_gaps(conn: psycopg.Connection) -> tuple[list[tuple[int, str]], int]:
    """Live securities whose daily series contains an anomalous overnight gap, plus the count of
    delisted securities that also gapped but cannot be safely asked about."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH r AS (
                SELECT security_id, trade_date, close,
                       lag(close) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev
                FROM price_bars_daily
            ), gapped AS (
                SELECT DISTINCT r.security_id
                FROM r
                WHERE r.prev IS NOT NULL AND r.prev > 0
                  AND (r.close / r.prev < %s OR r.close / r.prev > %s)
            )
            SELECT s.id, s.symbol, s.delisted_at IS NOT NULL
            FROM gapped g JOIN securities s ON s.id = g.security_id
            ORDER BY s.symbol
            """,
            (GAP_LOW, GAP_HIGH),
        )
        rows = cur.fetchall()
    live = [(r[0], r[1]) for r in rows if not r[2]]
    return live, len(rows) - len(live)


def candidates_all(conn: psycopg.Connection) -> tuple[list[tuple[int, str]], int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.symbol, s.delisted_at IS NOT NULL FROM securities s "
            "WHERE EXISTS (SELECT 1 FROM price_bars_daily d WHERE d.security_id = s.id) "
            "ORDER BY s.symbol"
        )
        rows = cur.fetchall()
    live = [(r[0], r[1]) for r in rows if not r[2]]
    return live, len(rows) - len(live)


def candidates_named(conn: psycopg.Connection, symbols: list[str]) -> tuple[list[tuple[int, str]], int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol FROM securities WHERE delisted_at IS NULL AND symbol = ANY(%s) ORDER BY symbol",
            ([s.upper() for s in symbols],),
        )
        return [(r[0], r[1]) for r in cur.fetchall()], 0


# ── provider ──────────────────────────────────────────────────────────────────────────────────
def fetch_actions(symbol: str) -> tuple[list[tuple[date, float]], list[tuple[date, float]]]:
    """(splits, dividends) for one symbol as (ex_date, value) pairs.

    Raises ProviderError when the provider cannot be asked. "Could not ask" and "asked, and there
    were none" must be distinguishable: the caller counts and reports the former and fails the run
    on it, because a swallowed failure here becomes split_adj_factor = 1 downstream (B-1).
    """
    try:
        import yfinance as yf
    except ModuleNotFoundError as exc:
        # Without the guard this is a raw traceback deep inside a loop; with it, one clear line.
        raise ProviderError(
            "yfinance is not installed — run via bin/db_corporate_actions.sh (rh-actions image)"
        ) from exc

    splits: list[tuple[date, float]] = []
    divs: list[tuple[date, float]] = []
    try:
        t = yf.Ticker(symbol)
        # The .splits/.dividends properties fetch lazily, so the iteration itself can raise.
        # `except Exception` is deliberate and narrow-by-translation: yfinance's failure surface
        # (requests exceptions, JSON decode errors, its own wrappers) is not a stable public set,
        # so everything is translated into ONE typed, counted, non-silent channel instead of a
        # pretend-precise tuple that silently misses the next wrapper change.
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
    except Exception as exc:  # translated to ProviderError — counted and surfaced by the caller
        raise ProviderError(f"{symbol}: provider error: {exc}") from exc
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
        cands, n_delisted = candidates_named(conn, args.symbols)
    elif args.candidates == "all":
        cands, n_delisted = candidates_all(conn)
    else:
        warn_if_archive_incomplete(conn)
        cands, n_delisted = candidates_from_gaps(conn)

    if n_delisted:
        logger.warning(
            "%d delisted securit(y/ies) excluded from the fetch: yfinance resolves a symbol to its "
            "CURRENT holder, so asking it about a dead identity would attribute the wrong issuer's "
            "actions. Their bars stay unadjusted until an identity-aware feed supplies them.",
            n_delisted,
        )
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

    n_splits = n_divs = with_actions = errors = stale_conflicts = 0
    provider_failures: list[str] = []
    consecutive_failures = 0
    started = time.monotonic()

    for i, (sec_id, symbol) in enumerate(cands, 1):
        try:
            splits, divs = fetch_actions(symbol)
        except ProviderError as exc:
            # Counted and loud, never silent: "could not ask" must stay distinguishable from
            # "no actions" all the way to the exit code (B-1).
            provider_failures.append(symbol)
            consecutive_failures += 1
            logger.warning("%s", exc)
            # Only short-circuit when NOTHING has succeeded yet (failures == candidates seen):
            # a mid-run rate-limit burst after real successes should keep going and be reported.
            if consecutive_failures >= CONSECUTIVE_PROVIDER_FAILURES_ABORT and consecutive_failures == i:
                logger.error(
                    "first %d provider calls ALL failed — this is a connectivity/wrapper problem "
                    "(no egress? wrong wrapper? hard rate limit), not %d coincidentally bad "
                    "symbols. Aborting instead of burning hours. Nothing partial was corrupted: "
                    "fetch is additive and adjust has not run.",
                    consecutive_failures, consecutive_failures,
                )
                return EXIT_CONNECTION
            continue
        consecutive_failures = 0
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
                    if cur.rowcount:
                        n_splits += 1
                    else:
                        stale_conflicts += _warn_if_conflict_differs(
                            cur, sec_id, symbol, "split", ex_date, "split_ratio", ratio)
                for ex_date, amt in divs:
                    cur.execute(
                        "INSERT INTO corporate_actions (security_id, action_type, ex_date, cash_amount, source_id) "
                        "VALUES (%s,'cash_dividend',%s,%s,%s) ON CONFLICT (security_id, action_type, ex_date) DO NOTHING",
                        (sec_id, ex_date, amt, source_id),
                    )
                    if cur.rowcount:
                        n_divs += 1
                    else:
                        stale_conflicts += _warn_if_conflict_differs(
                            cur, sec_id, symbol, "cash_dividend", ex_date, "cash_amount", amt)
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
        "fetched — %d securities had actions, %d splits, %d dividends, %d insert errors, "
        "%d provider failures, %d stale-conflict warnings, %.0fs",
        with_actions, n_splits, n_divs, errors, len(provider_failures), stale_conflicts,
        time.monotonic() - started,
    )
    if errors:
        logger.warning("%d symbol(s) failed to insert — see warnings above", errors)
    if provider_failures:
        shown = ", ".join(provider_failures[:25])
        more = f" … and {len(provider_failures) - 25} more" if len(provider_failures) > 25 else ""
        logger.error(
            "%d symbol(s) could NOT be asked (provider failures): %s%s. These securities may have "
            "unrecorded actions — re-run `fetch --symbols …` for them before trusting `adjust`. "
            "Exiting non-zero: an incomplete fetch must not read as success.",
            len(provider_failures), shown, more,
        )
        return EXIT_VALIDATION
    return EXIT_OK


def _warn_if_conflict_differs(
    cur: psycopg.Cursor, sec_id: int, symbol: str, action_type: str,
    ex_date: date, value_col: str, fetched: float,
) -> int:
    """After an ON CONFLICT DO NOTHING no-op, warn if the stored value differs from the fetched one.

    Returns 1 when it differs (a provider revision, or an unrepresentable second same-type action
    on one ex_date), else 0. The stored row is deliberately NOT overwritten — corporate_actions is
    provenance-bearing history and a revision policy is a schema decision — but the disagreement
    must be visible, not silently dropped (loaders review S-5; same idiom as the FRED revision fix).
    """
    # value_col is a hardcoded literal at both call sites ('split_ratio' / 'cash_amount'), never
    # external input — the f-string interpolates an identifier, all values are bound parameters.
    cur.execute(
        f"SELECT {value_col} FROM corporate_actions "
        "WHERE security_id = %s AND action_type = %s AND ex_date = %s",
        (sec_id, action_type, ex_date),
    )
    row = cur.fetchone()
    if row is None:  # pragma: no cover — conflict implies the row exists
        return 0
    stored = float(row[0])
    if math.isclose(stored, fetched, rel_tol=1e-9, abs_tol=1e-10):
        return 0
    logger.warning(
        "%s: %s on %s already recorded with %s=%s but the provider now says %s — value NOT "
        "updated; investigate (provider revision, or two same-type actions on one ex-date).",
        symbol, action_type, ex_date, value_col, stored, fetched,
    )
    return 1


# ── adjustment ────────────────────────────────────────────────────────────────────────────────
def cmd_adjust(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    """Populate split_adj_factor / adj_close, bounded to a declared as-of date.

    Idempotent — safe to re-run after loading more actions or more bars.

    THE AS-OF BOUND (semantics review B-S1, migration 007): factors are computed from splits with
    ex_date in (bar_date, adjustment_as_of] only, where adjustment_as_of = the archive's last
    covered session. The provider returns splits through TODAY; without the upper bound, 527
    post-archive splits contaminated 308,709 bars' price LEVELS with information nobody could have
    had (PAVS closed $1.04 on 2025-09-30 with a stored adj_close of $124,800, and 196,909 bars
    crossed a $5 floor on future reverse splits). Returns were never affected — a split outside
    both endpoints cancels in the factor ratio — but a level screen was poisoned. The bound is
    recorded in price_adjustment_state so the contamination window is explicit and auditable.
    """
    started = time.monotonic()

    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM price_bars_daily")
        as_of = cur.fetchone()[0]
    if as_of is None:
        raise LoadError("price_bars_daily is empty — run the daily loader before adjusting")

    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT security_id) FROM corporate_actions WHERE action_type='split'")
        n_split_secs = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*), count(DISTINCT security_id) FROM corporate_actions "
            "WHERE action_type = 'split' AND ex_date > %s", (as_of,),
        )
        n_future, n_future_secs = cur.fetchone()
    logger.info("%d securities carry at least one split; adjustment_as_of = %s", n_split_secs, as_of)
    if n_future:
        logger.info(
            "%d split(s) across %d securit(y/ies) have ex_date AFTER %s — EXCLUDED from every "
            "factor (they are the future relative to this archive). They remain stored and will "
            "enter the adjustment when the archive extends past their ex-date.",
            n_future, n_future_secs, as_of,
        )

    # Record the bound BEFORE writing factors: a factor column must never exist without its
    # declared as-of, or the contamination window stops being auditable.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO price_adjustment_state (id, adjustment_as_of, adjusted_at) "
            "VALUES (1, %s, now()) "
            "ON CONFLICT (id) DO UPDATE SET adjustment_as_of = EXCLUDED.adjustment_as_of, "
            "adjusted_at = EXCLUDED.adjusted_at",
            (as_of,),
        )

    # Securities WITH splits: the real adjustment. Small set, so the per-row function call is cheap.
    #
    # The FACTOR is always written; adj_close only where the LEVEL is meaningful. Serial
    # reverse-splitters (WHLR 16 splits, UVXY 13, NUWE whose 8 splits multiply to 7.71e-13
    # unbounded — the as-of bound keeps the STORED minimum far higher) imply
    # adjusted prices around 1e13. NUMERIC(30,10) would HOLD those (it tops out just below 1e20) —
    # the cutoff is not representability but meaning: no instrument has a trillion-dollar
    # per-share price, and 006's contract is that NULL says "use the factor" while a stored absurd
    # level says nothing. The factor carries the full information, and returns computed as
    # (close/f) ratios never need the level. See migrations 006 and 007.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE price_bars_daily d
            SET split_adj_factor = f.factor,
                adj_close = CASE
                    WHEN d.close / f.factor <= %s THEN ROUND(d.close / f.factor, 10)
                    ELSE NULL
                END
            FROM (
                SELECT d2.security_id, d2.trade_date,
                       split_factor_between(d2.security_id, d2.trade_date, %s) AS factor
                FROM price_bars_daily d2
                WHERE d2.security_id IN (
                    SELECT DISTINCT security_id FROM corporate_actions WHERE action_type='split')
            ) f
            WHERE d.security_id = f.security_id AND d.trade_date = f.trade_date
            """,
            (MAX_MEANINGFUL_ADJ_CLOSE, as_of),
        )
        adjusted = cur.rowcount

    # Securities whose factors are STALE: rows carrying a non-1 factor while the security no
    # longer has any in-bound split — actions were re-attributed (delisting splice), deleted, or
    # newly excluded by the as-of bound. Without this reset a removed cause leaves its effect
    # behind forever.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE price_bars_daily d
            SET split_adj_factor = 1, adj_close = d.close
            WHERE d.split_adj_factor IS NOT NULL AND d.split_adj_factor <> 1
              AND d.security_id NOT IN (
                  SELECT DISTINCT security_id FROM corporate_actions WHERE action_type = 'split')
            """
        )
        if cur.rowcount:
            logger.info("reset %s stale-factor bars whose securities no longer carry splits", f"{cur.rowcount:,}")

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

    Also cross-checks every stored factor against a recomputation bounded by the recorded
    adjustment_as_of — the test that proves the B-S1 lookahead stays gone: a factor computed from
    an unbounded split product disagrees with the bounded recomputation the moment any security
    carries a post-as-of split.
    """
    # ── factor point-in-time cross-check ──────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT adjustment_as_of FROM price_adjustment_state WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        logger.error("price_adjustment_state is empty — `adjust` has not run under migration 007; "
                     "any stored factors are unaudited. Run `adjust` first.")
        return EXIT_VALIDATION
    as_of = row[0]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM price_bars_daily d
            WHERE d.security_id IN (
                SELECT DISTINCT security_id FROM corporate_actions WHERE action_type = 'split')
              AND d.split_adj_factor IS DISTINCT FROM
                  split_factor_between(d.security_id, d.trade_date, %s)
            """,
            (as_of,),
        )
        n_factor_mismatch = cur.fetchone()[0]
    if n_factor_mismatch:
        logger.error(
            "%s bar(s) carry a factor that does NOT equal the split product bounded by "
            "adjustment_as_of=%s — stale or lookahead-contaminated adjustment. Re-run `adjust`.",
            f"{n_factor_mismatch:,}", as_of,
        )
        return EXIT_VALIDATION
    # The other cohort — securities with NO recorded split — is checked too, so the claim below
    # really covers every bar in the table. (The earlier version recomputed only split-bearing
    # securities, ~13% of bars, while its log line said "every stored factor".)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM price_bars_daily d
            WHERE d.security_id NOT IN (
                SELECT DISTINCT security_id FROM corporate_actions WHERE action_type = 'split')
              AND (d.split_adj_factor IS DISTINCT FROM 1 OR d.adj_close IS DISTINCT FROM d.close)
            """
        )
        n_nonsplit_mismatch = cur.fetchone()[0]
    if n_nonsplit_mismatch:
        logger.error(
            "%s bar(s) of split-free securities carry a factor != 1 or adj_close != close — "
            "stale adjustment (actions re-attributed or deleted). Re-run `adjust`.",
            f"{n_nonsplit_mismatch:,}",
        )
        return EXIT_VALIDATION
    logger.info(
        "factor cross-check: split-security bars match the as-of-bounded product and split-free "
        "bars carry factor 1 — the whole table, both cohorts (as_of=%s)", as_of,
    )

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
    except psycopg.OperationalError as exc:
        # A dropped connection mid-run is an infrastructure failure, not a SQL one.
        logger.error("connection lost: %s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("database error: %s", exc)
        return EXIT_SQL
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
