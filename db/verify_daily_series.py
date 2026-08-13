"""Per-session, multi-symbol verification of the daily series — the test "+100.4% ≈ SPY" was not.

WHY (semantics review S-S6)
    The old validation compared two endpoint prices five years apart. Its measured sensitivity:
    a consistent ONE-session misalignment still lands on 97.0%, and a 20-session misalignment on
    104.9% — both inside any tolerance anyone would set on "about 100%". It also quietly
    confirmed the price-only number (B-S2) rather than the pipeline. This module replaces it
    with the per-session reconciliation the review specified:

      1. DATE ALIGNMENT (offline)   — per reference symbol, the set of session dates equals the
                                      calendar's trading days minus the globally-registered gap
                                      dates, over the symbol's own bar range. Catches any
                                      per-symbol missing or extra session. Two things it cannot
                                      catch, stated plainly: a uniform whole-archive shift onto
                                      adjacent trading days (every set still matches), and a
                                      universe-wide coverage loss (it lands in the global-gap
                                      set, which is derived from the data and only LOGGED here —
                                      the count is the tell an operator watches).
      5. FACTOR PIT CROSS-CHECK (offline) — all 12,840,439 bars: split-bearing securities'
                                      factors are recomputed against the split product bounded by
                                      the recorded adjustment_as_of, and every other security is
                                      verified to carry factor exactly 1 with adj_close = close.
                                      An unbounded (lookahead) recomputation fails this the
                                      moment any security carries a post-as-of split. This is a
                                      STALENESS check against our own actions table, not an
                                      independent-provider check — checks 2-4 are the
                                      provider-facing half.
      7. GAP AUDIT (offline)        — the universe-wide splice tripwire (B-N2): every internal
                                      hole of >= 10 missed covered sessions whose split-adjusted
                                      cross-gap ratio falls outside the audit band must carry a
                                      resolved disposition in price_gap_audit. An unaudited or
                                      unresolved hole FAILS — a possible two-issuer splice is
                                      never silently tolerated. (Replaces the old claim that this
                                      module's 20 reference names were the tripwire for recycled
                                      tickers; they never could be — recycling happens in
                                      delisted small caps, a cohort with zero overlap with them.)
      2. PER-SESSION CLOSE BOUND (--provider) — max |bps| and mean signed diff vs the official
                                      close, within DOCUMENTED bounds for the 15:59 basis.
      3. PAIRED RETURN DISTRIBUTION (--provider) — sd(our_ret − official_ret) and correlation.
                                      This is what a level bound alone lets through, and it maps
                                      directly onto Sharpe error.
      4. REALIZED VOL (--provider)  — annualised vol within a relative band; the denominator of
                                      every ratio.
      6. VOLUME BAND (--provider)   — median ours/official inside a documented band, so a silent
                                      change in what the session window includes is caught.

    PROVIDER BASIS (checks 2-6): yfinance with auto_adjust=False returns Close AND Volume on the
    CURRENT split basis (measured, NVDA around the 2024-06-10 10:1 split: provider pre-split
    closes are ÷10 and volumes ×10 versus the raw prints). Our adj_close is pinned to
    adjustment_as_of. So like is compared with like by scaling the provider's series onto the
    as-of basis with the product of OUR recorded post-as-of splits (residual = 1 for every
    symbol with none): price compare = adj_close vs their_close × residual; volume compare =
    volume × split_adj_factor × residual vs their_volume. A residual mismatch (a post-as-of
    split the actions table lacks) fails check 2 loudly — the fix is a re-fetch, not a wider
    bound.

    THRESHOLDS ARE PINNED TO THE CURRENT, DOCUMENTED 15:59-ET-CLOSE BASIS (see migration 007's
    comment on price_bars_daily.close): worst observed SPY close deviation is 95.7 bps, paired
    return sd 4.48 (SPY) / 6.13 (MSFT) bps/day, volume median ratio 0.853 (SPY) / 0.772 (MSFT —
    0.022 above the band floor, the tightest measured headroom). When an official-close source is
    loaded, TIGHTEN: close bound → ~1 bps, return sd → ~1 bps/day, volume band → ~1.0.

Offline checks need only the database. --provider adds yfinance calls (egress required — run via
LOADER_SCRIPT=/repo/db/verify_daily_series.py bin/db_corporate_actions.sh).

Exit codes: 0 all checks pass · 1 a check failed · 2 SQL failure · 3 connection/provider failure.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import statistics
import sys
from datetime import date, timedelta

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    print("verify_daily_series: psycopg (v3) required", file=sys.stderr)
    raise SystemExit(3) from None

# Shared with the audit tool so the tripwire and the audit can never disagree about what a hole
# is or which dispositions count as resolved.
import load_delistings as ldel

logger = logging.getLogger("verify_series")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

# Liquid reference names across sectors and exchanges, deliberately including split cases (NVDA,
# AMZN, GOOGL, TSLA) and non-splitters, ETFs and single names. NOT all continuously listed: META
# is the live post-splice identity (Meta Platforms, bars from 2022-06-09) — its predecessor on
# the recycled ticker was a Roundhill ETF, delisted by the B-S5 splice. Check 1 windows to each
# symbol's own bar range, so a late first bar is fine and a hole inside the range is not.
REFERENCE_SYMBOLS = (
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "XOM", "KO", "JNJ", "PG", "WMT", "UNH", "HD", "V", "MA",
)

# Bounds for the CURRENT 15:59 close basis — see the module docstring for the tighten-later plan
# and the measured headroom (SPY corr 0.99925 sits 0.00025 above the floor; MSFT volume 0.772
# sits 0.022 above the band floor).
CLOSE_MAX_ABS_BPS = 100.0
CLOSE_MEAN_SIGNED_BPS = 1.0
# 25 bps/day, NOT the 8 the first draft shipped: that bound had never been exercised against a
# split name (the raw-vs-adjusted bug failed check 2 first and the loop stopped there). Measured
# per-symbol return-diff sds on the 15:59 basis include AMZN 14.8 and TSLA 18.5 bps/day — high-
# beta single names carry more close-definition noise than SPY's 4.5. 25 covers the measured
# reference set with headroom while still catching an adjustment error (a missed 2:1 split is a
# ~7,000 bps one-day return difference).
RETURN_DIFF_SD_MAX_BPS = 25.0
RETURN_CORR_MIN = 0.999
VOL_REL_TOLERANCE = 0.03
VOLUME_MEDIAN_BAND = (0.75, 0.98)


class CheckFailure(Exception):
    """A verification check failed — collected, reported, exit 1."""


def connect() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(EXIT_CONNECTION)
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-verify-series")
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
    return conn


def _series(conn: psycopg.Connection, symbol: str) -> list[tuple[date, float, float | None, float, int]]:
    """(trade_date, close, adj_close, split_adj_factor, volume) for the LIVE holder of symbol."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.trade_date, d.close::float8, d.adj_close::float8, "
            "d.split_adj_factor::float8, d.volume "
            "FROM price_bars_daily d JOIN securities s ON s.id = d.security_id "
            "WHERE s.symbol = %s AND s.delisted_at IS NULL ORDER BY d.trade_date",
            (symbol,),
        )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in cur.fetchall()]


# ── check 1: exact date alignment ─────────────────────────────────────────────────────────────
def check_alignment(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        # Globally-registered gaps: calendar sessions where the WHOLE archive has no bars
        # (the corrupt-member holes). Registered means visible — they are excused per symbol,
        # but a symbol missing a session the rest of the universe has is NOT excused. Because
        # this set is derived from the data it checks, a NEW universe-wide coverage loss lands
        # here instead of failing; the logged count (15 as of 2026-07-29, the December-2024
        # hole) is what an operator watches for movement.
        cur.execute(
            """
            SELECT c.trade_date FROM market_calendar c
            WHERE c.is_trading_day
              AND c.trade_date BETWEEN (SELECT min(trade_date) FROM price_bars_daily)
                                   AND (SELECT max(trade_date) FROM price_bars_daily)
              AND NOT EXISTS (SELECT 1 FROM price_bars_daily d WHERE d.trade_date = c.trade_date)
            """
        )
        global_gaps = {r[0] for r in cur.fetchall()}
    logger.info("check 1: %d globally-registered gap session(s)", len(global_gaps))

    for symbol in REFERENCE_SYMBOLS:
        rows = _series(conn, symbol)
        if not rows:
            raise CheckFailure(f"check 1: reference symbol {symbol} has no bars")
        have = {r[0] for r in rows}
        lo, hi = min(have), max(have)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date FROM market_calendar "
                "WHERE is_trading_day AND trade_date BETWEEN %s AND %s",
                (lo, hi),
            )
            expected = {r[0] for r in cur.fetchall()}
        missing = expected - have - global_gaps
        extra = have - expected
        if missing:
            raise CheckFailure(
                f"check 1: {symbol} missing {len(missing)} session(s) the calendar expects "
                f"(first: {sorted(missing)[:5]})"
            )
        if extra:
            raise CheckFailure(
                f"check 1: {symbol} has bars on {len(extra)} non-trading day(s) "
                f"(first: {sorted(extra)[:5]}) — the calendar or the loader is wrong"
            )
    logger.info("check 1 PASS: all %d reference symbols align exactly with the calendar over "
                "their own bar ranges", len(REFERENCE_SYMBOLS))


# ── check 5: factor point-in-time cross-check ─────────────────────────────────────────────────
def check_factor_pit(conn: psycopg.Connection) -> None:
    """Every one of the 12.8M stored factors, in two exhaustive halves.

    Split-bearing securities' bars (1,648,849 at last count) are recomputed against the as-of-
    bounded split product; every other bar (11,191,590) is verified to carry factor exactly 1
    with adj_close = close. Together the two cohorts are the whole table — the earlier version
    of this check ran only the first half while claiming the whole. NOTE this recomputes from
    OUR corporate_actions with OUR split_factor_between — it proves the stored columns are not
    stale against the recorded actions, not that the actions are right; provider-facing
    verification is checks 2-4.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT adjustment_as_of FROM price_adjustment_state WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            raise CheckFailure("check 5: price_adjustment_state is empty — adjust has not run under 007")
        as_of = row[0]
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
        mismatches = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE x.mismatch), count(*) FROM (
                SELECT (d.split_adj_factor IS DISTINCT FROM 1
                        OR d.adj_close IS DISTINCT FROM d.close) AS mismatch
                FROM price_bars_daily d
                WHERE d.security_id NOT IN (
                    SELECT DISTINCT security_id FROM corporate_actions WHERE action_type = 'split')
            ) x
            """
        )
        nonsplit_mismatches, nonsplit_total = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM corporate_actions WHERE action_type='split' AND ex_date > %s",
            (as_of,),
        )
        future_splits = cur.fetchone()[0]
    if mismatches:
        raise CheckFailure(
            f"check 5: {mismatches} split-security bar(s) disagree with the as-of-bounded factor "
            f"(as_of={as_of}) — stale or lookahead-contaminated; re-run adjust"
        )
    if nonsplit_mismatches:
        raise CheckFailure(
            f"check 5: {nonsplit_mismatches} bar(s) of split-free securities carry a factor != 1 "
            "or adj_close != close — stale adjustment (re-attributed or deleted actions); re-run adjust"
        )
    logger.info(
        "check 5 PASS: split-security bars match the bounded product and the other %s bars carry "
        "factor 1 with adj_close = close (as_of=%s; %d post-as-of split(s) on record, correctly "
        "excluded). Staleness proven against our own actions table; provider-facing checks are 2-4.",
        f"{nonsplit_total:,}", as_of, future_splits,
    )


# ── check 7: universe-wide gap-audit tripwire ─────────────────────────────────────────────────
def check_gap_audit(conn: psycopg.Connection) -> None:
    """Every current internal hole >= the audit floor with an out-of-band cross-gap ratio must
    carry a RESOLVED disposition in price_gap_audit. This is the tripwire for recycled tickers
    at ANY gap length — universe-wide, unlike the 20 reference names above, which recycling
    (a delisted-small-cap phenomenon) never touches. Tunable via load_delistings' audit
    constants; overridable per hole via an explicit halt_accepted disposition."""
    floor = ldel.AUDIT_MIN_MISSED_SESSIONS
    lo, hi = ldel.AUDIT_RATIO_LOW, ldel.AUDIT_RATIO_HIGH
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                count(*) FILTER (WHERE h.missed_sessions >= %(f)s)                     AS holes,
                count(*) FILTER (WHERE h.missed_sessions >= %(f)s AND oob)             AS oob,
                count(*) FILTER (WHERE h.missed_sessions >= %(f)s AND oob
                                   AND a.id IS NULL)                                   AS unaudited,
                count(*) FILTER (WHERE h.missed_sessions >= %(f)s AND oob
                                   AND a.disposition = ANY(%(nonterm)s))               AS unresolved,
                count(*) FILTER (WHERE h.missed_sessions BETWEEN 1 AND %(f)s - 1
                                   AND oob)                                            AS subfloor_oob
            FROM (SELECT *, (adj_ratio < %(lo)s OR adj_ratio > %(hi)s) AS oob
                  FROM ({ldel.HOLE_DETECTION_SQL}) h0) h
            LEFT JOIN price_gap_audit a
                   ON a.security_id = h.security_id AND a.gap_start = h.gap_start
            """,
            {"f": floor, "lo": lo, "hi": hi, "nonterm": list(ldel.NON_TERMINAL_DISPOSITIONS)},
        )
        holes, oob, unaudited, unresolved, subfloor_oob = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM price_gap_audit WHERE disposition = 'halt_consistent'"
        )
        halt_consistent = cur.fetchone()[0]
    logger.info(
        "check 7: %d hole(s) >= %d missed sessions (%d outside ratio band [%s, %s]); %d sub-floor "
        "out-of-band hole(s) below the classification floor (counted, see load_delistings.py); "
        "%d in-band hole(s) recorded halt_consistent — the counted blind spot a ratio cannot see",
        holes, floor, oob, lo, hi, subfloor_oob, halt_consistent,
    )
    if unaudited:
        raise CheckFailure(
            f"check 7: {unaudited} hole(s) >= {floor} missed sessions with an out-of-band "
            f"cross-gap ratio have NO price_gap_audit row — new since the last audit. Run: "
            "LOADER_SCRIPT=/repo/db/load_delistings.py bin/db_corporate_actions.sh audit"
        )
    if unresolved:
        raise CheckFailure(
            f"check 7: {unresolved} audited hole(s) carry an unresolved disposition "
            f"({', '.join(ldel.NON_TERMINAL_DISPOSITIONS)}) — a possible two-issuer splice is "
            "unclassified. Resolve via audit --provider / splice --from-audit / fetch --symbols "
            "(split_missing) / an explicit halt_accepted override."
        )
    logger.info("check 7 PASS: every out-of-band hole >= %d missed sessions is audited and resolved", floor)


# ── provider-backed checks 2, 3, 4, 6 ─────────────────────────────────────────────────────────
def check_provider(conn: psycopg.Connection, symbols: tuple[str, ...]) -> list[str]:
    """Returns the list of per-symbol check failures (empty = all pass).

    Failures are COLLECTED per symbol, never raised on the first: a tool that stops at symbol 6
    of 20 teaches its operator nothing about the other 14 (that defect hid this module's own
    raw-vs-adjusted bug behind NVDA's failure). Provider CONNECTIVITY failures still exit 3
    immediately — a dead wire is not a data finding.
    """
    try:
        import yfinance as yf
    except ModuleNotFoundError:
        print("--provider needs yfinance (run via bin/db_corporate_actions.sh)", file=sys.stderr)
        raise SystemExit(EXIT_CONNECTION) from None

    with conn.cursor() as cur:
        cur.execute("SELECT adjustment_as_of FROM price_adjustment_state WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        return ["provider checks: price_adjustment_state is empty — run adjust first"]
    as_of = row[0]

    failures: list[str] = []
    for symbol in symbols:
        ours = _series(conn, symbol)
        if not ours:
            failures.append(f"provider checks: {symbol} has no bars")
            continue
        # Residual: OUR recorded splits after the as-of. The provider's series is on its current
        # basis; ours is pinned to as_of. Scaling theirs by this puts both on the as-of basis.
        # If the provider has a post-as-of split we never fetched, check 2 fails loudly — the
        # remedy is a fetch, not a wider bound.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(exp(sum(ln(ca.split_ratio))), 1)::float8 "
                "FROM corporate_actions ca JOIN securities s ON s.id = ca.security_id "
                "WHERE s.symbol = %s AND s.delisted_at IS NULL "
                "  AND ca.action_type = 'split' AND ca.ex_date > %s",
                (symbol, as_of),
            )
            residual = cur.fetchone()[0]
        if residual != 1.0:
            logger.info("%s: %s post-as-of split residual applied to the provider series",
                        symbol, residual)
        lo, hi = ours[0][0], ours[-1][0]
        try:
            # yfinance's `end` is exclusive — add a day so the final session pairs too.
            hist = yf.Ticker(symbol).history(
                start=lo.isoformat(), end=(hi + timedelta(days=1)).isoformat(),
                auto_adjust=False, actions=False,
            )
        except Exception as exc:  # provider surface is not a stable exception set
            print(f"provider fetch failed for {symbol}: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_CONNECTION) from exc
        theirs = {
            ts.date(): (float(row["Close"]), int(row["Volume"]))
            for ts, row in hist.iterrows()
        }
        failures.extend(_check_one_symbol(symbol, ours, theirs, residual))
    return failures


def _check_one_symbol(
    symbol: str,
    ours: list[tuple[date, float, float | None, float, int]],
    theirs: dict[date, tuple[float, int]],
    residual: float,
) -> list[str]:
    """Checks 2/3/4/6 for one symbol against a provider series on the CURRENT split basis.

    `ours` rows are (trade_date, close, adj_close, split_adj_factor, volume) — raw close and
    as-traded volume, with adj_close/factor pinned to adjustment_as_of. `residual` is the
    product of recorded post-as-of splits (1 when none). Comparisons are same-basis by
    construction: our adj_close vs their close × residual; our volume × factor × residual vs
    their volume (the provider adjusts BOTH price and volume — measured, see module docstring).
    Bars with adj_close NULL (levels beyond $1e12) are skipped and counted — the factor, not
    the level, carries their information.
    """
    failures: list[str] = []
    skipped_null_adj = sum(1 for _d, _c, a, _f, _v in ours if a is None)
    paired = [
        (d, a, theirs[d][0] * residual, v * f * residual, theirs[d][1])
        for d, _c, a, f, v in ours
        if d in theirs and a is not None
    ]
    if skipped_null_adj:
        logger.info("%s: %d bar(s) with NULL adj_close skipped (factor-only levels)",
                    symbol, skipped_null_adj)
    if len(paired) < 250:
        failures.append(f"check 2: {symbol}: only {len(paired)} paired sessions — cannot judge")
        return failures

    # 2 — per-session close bound (both sides on the as-of split basis).
    diffs_bps = [(c / t - 1) * 1e4 for _d, c, t, _v, _tv in paired]
    worst = max(abs(x) for x in diffs_bps)
    mean_signed = statistics.fmean(diffs_bps)
    if worst > CLOSE_MAX_ABS_BPS or abs(mean_signed) > CLOSE_MEAN_SIGNED_BPS:
        failures.append(
            f"check 2: {symbol}: close vs official worst {worst:.1f} bps "
            f"(bound {CLOSE_MAX_ABS_BPS}), mean {mean_signed:+.3f} bps "
            f"(bound ±{CLOSE_MEAN_SIGNED_BPS})"
        )

    # 3 — paired daily-return distribution.
    our_ret = [paired[i][1] / paired[i - 1][1] - 1 for i in range(1, len(paired))]
    their_ret = [paired[i][2] / paired[i - 1][2] - 1 for i in range(1, len(paired))]
    diff_ret = [a - b for a, b in zip(our_ret, their_ret, strict=True)]
    sd_bps = statistics.stdev(diff_ret) * 1e4
    corr = _corr(our_ret, their_ret)
    if sd_bps > RETURN_DIFF_SD_MAX_BPS or corr < RETURN_CORR_MIN:
        failures.append(
            f"check 3: {symbol}: return-diff sd {sd_bps:.2f} bps/day "
            f"(bound {RETURN_DIFF_SD_MAX_BPS}), corr {corr:.5f} (min {RETURN_CORR_MIN})"
        )

    # 4 — realized vol agreement.
    vol_ours = statistics.stdev(our_ret) * math.sqrt(252)
    vol_theirs = statistics.stdev(their_ret) * math.sqrt(252)
    rel = vol_ours / vol_theirs - 1
    if abs(rel) > VOL_REL_TOLERANCE:
        failures.append(
            f"check 4: {symbol}: annualised vol {vol_ours:.4f} vs official {vol_theirs:.4f} "
            f"({rel:+.2%}, tolerance ±{VOL_REL_TOLERANCE:.0%})"
        )

    # 6 — volume band (our as-traded volume scaled to the provider's current share basis).
    ratios = sorted(v / tv for _d, _c, _t, v, tv in paired if tv > 0)
    med = ratios[len(ratios) // 2]
    if not (VOLUME_MEDIAN_BAND[0] <= med <= VOLUME_MEDIAN_BAND[1]):
        failures.append(
            f"check 6: {symbol}: median volume ratio {med:.3f} outside {VOLUME_MEDIAN_BAND} "
            "— the session window's contents changed"
        )
    if not failures:
        logger.info(
            "provider checks PASS %s: close worst %.1f bps / mean %+.3f bps, ret-sd %.2f bps, "
            "corr %.5f, vol %+.2f%%, volume median %.3f",
            symbol, worst, mean_signed, sd_bps, corr, rel * 100, med,
        )
    return failures


def _corr(a: list[float], b: list[float]) -> float:
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return cov / (va * vb) if va and vb else float("nan")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verify_daily_series")
    p.add_argument("--provider", action="store_true",
                   help="also run the yfinance-backed checks 2/3/4/6 (egress required)")
    p.add_argument("--symbols", nargs="*", help="override the reference symbol set")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    symbols = tuple(s.upper() for s in args.symbols) if args.symbols else REFERENCE_SYMBOLS
    conn = connect()
    failures: list[str] = []
    try:
        for check in (check_alignment, check_factor_pit, check_gap_audit):
            try:
                check(conn)
            except CheckFailure as exc:
                failures.append(str(exc))
                logger.error("%s", exc)
        if args.provider:
            for failure in check_provider(conn, symbols):
                failures.append(failure)
                logger.error("%s", failure)
    except psycopg.OperationalError as exc:
        logger.error("connection lost: %s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("database error: %s", exc)
        return EXIT_SQL
    finally:
        conn.close()

    if failures:
        logger.error("%d verification check(s) FAILED", len(failures))
        return EXIT_VALIDATION
    logger.info("all verification checks passed")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
