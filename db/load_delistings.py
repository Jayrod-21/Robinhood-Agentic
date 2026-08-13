"""Delisting lifecycle for `securities`: splice recycled tickers, mark dead names.

WHY (semantics review B-S5)
    `delisted_at` was never set by any loader, so the schema's own re-listing rule
    (001: "a symbol re-appearing after its previous holder was delisted is a NEW row") never
    fired. Measured consequences on the live archive: 283 securities carried an internal hole
    > 180 days — two different issuers spliced into one price series (FLY +255% and FLXN +174%
    "single-session" returns that never happened, TOT: Total SA's series continuing as a new
    issuer at −59%) — 22 of them with the CURRENT issuer's splits applied to the PRIOR issuer's
    prices; and 7,621 securities (39.5%) had no bar in the archive's final week with nothing
    marking them dead, so a backtest could hold a position that never realises its loss — the
    single most return-inflating bug in this class.

COMMANDS (all support --dry-run)
    audit   Enumerate every internal hole of >= --min-missed-sessions missed COVERED sessions
            (sessions where the rest of the archive has bars — the universe-wide December-2024
            hole contributes zero) into `price_gap_audit`, classified by evidence:

              * adj_ratio = (close_after / close_before) x recorded in-hole split ratios. Inside
                [--ratio-low, --ratio-high] -> 'halt_consistent': the move is explicable by one
                suspended issuer. This cohort is STORED AND COUNTED, not assumed benign — ratio
                evidence structurally cannot see a recycle onto a similar-priced issuer.
              * Outside the band -> 'pending_review': a discontinuity no recorded action
                explains. These MUST be resolved (see --provider) before verification passes —
                verify_daily_series.py check 7 fails while any non-terminal disposition remains.

            With --provider (egress required), each pending hole is checked against the
            provider's own history for the symbol — the evidence that separated the confirmed
            identity breaks (COHR, DBD, VRM, FNGU, FIG, AI) from real moves:
              history begins at/after the resume date (or inside the hole)  -> 'identity_break'
              history spans the pre-gap dates, provider cross-gap ratio in-band
                                                                            -> 'split_missing'
                 (the provider's basis folds in an action we never recorded — run
                  load_corporate_actions.py fetch --symbols <SYM>, adjust, then re-audit)
              history spans the pre-gap dates, provider ratio ALSO out-of-band
                                                                            -> 'continuity_confirmed'
              provider cannot speak for the symbol                          -> 'provider_unresolvable'

            WHY THE FLOOR IS 10 SESSIONS AND NOT A GUESSED GAP LENGTH: SEC Rule 12(k) caps a
            trading suspension at 10 business days and exchange halts are far shorter, so a
            security absent for MORE than 10 sessions the market traded is outside every
            routine-halt mechanism and must be classified. Sub-floor out-of-band moves are
            counted and logged each run (they are overwhelmingly real post-halt moves), never
            assumed empty. The ratio band [0.5, 2.0] is measured, not guessed: every provider-
            confirmed identity break in this archive sits outside it (ratios 0.04-220), while
            widening it excludes real single-issuer moves the audit must not flag.

    splice  Split a security at an internal hole into two identities: the pre-gap issuer keeps
            the old row and is delisted at the gap; the post-gap issuer becomes a NEW row holding
            the post-gap bars and ALL recorded corporate actions (the provider was asked by
            symbol, so its answers describe the CURRENT holder — that is precisely the
            mis-attribution being repaired). RE-RUN `load_corporate_actions.py adjust`
            AFTERWARDS: the pre-gap identity's factors are stale until its reset pass runs.

            --from-audit (the normal mode since 008): splice the holes `audit` classified
            'identity_break' or 'provider_unresolvable' — unresolvable defaults to splice
            because a fabricated cross-gap "return" is exactly the number this exists to kill,
            and splicing a genuinely-continuous-but-unprovable issuer merely truncates a series
            at a void no honest backtest could price across anyway. Each spliced hole's audit row
            becomes 'spliced'. --include-pending extends the splice to 'pending_review' rows for
            environments with no egress; it says so loudly and records it in the evidence text.

            --min-gap-days N (the pre-008 mode, kept for the historical 180/120-day passes and
            regression tests): splice every hole longer than N calendar days, no evidence
            required. Superseded by the audit flow — a bare length threshold cannot see a 47-day
            recycle like C3.ai taking Arlington Asset's "AI" ticker in December 2020.

            Splices are recorded in data_sources and price_gap_audit either way, so every one is
            auditable and reversible by hand if an identity is later proven continuous.

    infer   Absence-based delisting: a live security whose last bar is more than
            --confirm-sessions trading sessions (default 5) before the archive's last covered
            session gets delisted_at = last bar + 1 day. In a full-universe daily file, a listed
            security that prints NOTHING for a week is dead or suspended; either way a backtest
            must not hold it at a phantom price. delisted_at here is an inference from absence,
            not an authoritative date — `fmp` refines it where FMP knows better.

    fmp     Authoritative refinement from FMP's Delisted Companies endpoint (stable API, free
            tier — requires FMP_API_KEY in the environment). Pages newest-first at 100 rows per
            call; --max-pages (default 120) caps the call budget well inside the free tier's
            250/day. An FMP record is matched to OUR identity only when the symbol matches AND
            our last bar falls within [delistedDate - 60d, delistedDate): tickers are recycled
            on FMP's side too, and a date-window match is what keeps a 2019 delisting from
            marking the symbol's current holder dead.

Run order: audit → audit --provider → splice --from-audit → fmp → infer →
load_corporate_actions.py adjust → verify_daily_series.py (check 7 confirms nothing is left
unclassified).

Usage (via the egress wrapper; audit --provider and fmp need both the DB and the internet):
    LOADER_SCRIPT=/repo/db/load_delistings.py bin/db_corporate_actions.sh audit
    LOADER_SCRIPT=/repo/db/load_delistings.py bin/db_corporate_actions.sh audit --provider
    LOADER_SCRIPT=/repo/db/load_delistings.py bin/db_corporate_actions.sh splice --from-audit
    LOADER_SCRIPT=/repo/db/load_delistings.py bin/db_corporate_actions.sh fmp
    LOADER_SCRIPT=/repo/db/load_delistings.py bin/db_corporate_actions.sh infer
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    print("load_delistings: psycopg (v3) required", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("delistings")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

# Mirrors ck_securities_symbol (001). FMP symbols that cannot be normalized into this grammar are
# counted and skipped, never guessed at.
SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}(\.[A-Za-z0-9]{1,4}){0,2}$")

FMP_URL = "https://financialmodelingprep.com/stable/delisted-companies"
FMP_PAGE_SIZE = 100          # the stable endpoint caps at 100 regardless of ?limit
FETCH_ATTEMPTS = 4
FETCH_BACKOFF_BASE_S = 2.0

# ── audit constants (all overridable on the command line — tunable, observable, per Bar §7.2) ──
# 10 missed covered sessions: the one floor that is institutional fact rather than a guess — SEC
# Rule 12(k) caps a trading suspension at 10 business days, and exchange news/LULD halts are far
# shorter. A security absent LONGER than that while the rest of the archive traded is outside
# every routine-halt mechanism. Sub-floor out-of-band holes are counted and logged every run.
AUDIT_MIN_MISSED_SESSIONS = 10
# The evidence band, measured on this archive (2026-07-29): every provider-confirmed identity
# break sits far outside [0.5, 2.0] (adj ratios 0.04 to 220), and the unrecorded-reverse-split
# family (LIA*/LFA*, uniform ~9.4-10.5x) is outside it too. In-band does NOT prove continuity —
# a recycle onto a similar-priced issuer is invisible to a ratio — which is why in-band holes are
# stored as 'halt_consistent' rather than dropped.
AUDIT_RATIO_LOW = 0.5
AUDIT_RATIO_HIGH = 2.0
# Provider-evidence pass: abort when the first N lookups ALL fail (connectivity, not symbols —
# same reasoning as load_corporate_actions.CONSECUTIVE_PROVIDER_FAILURES_ABORT).
CONSECUTIVE_PROVIDER_FAILURES_ABORT = 15
# Polite pacing between provider history calls.
PROVIDER_CALL_SLEEP_S = 0.25

# Dispositions that mean "not yet resolved" — verify_daily_series.py check 7 fails while any
# audited hole carries one of these. Keep in lockstep with 008's CHECK constraint.
NON_TERMINAL_DISPOSITIONS = ("pending_review", "identity_break", "provider_unresolvable", "split_missing")
TERMINAL_DISPOSITIONS = ("halt_consistent", "continuity_confirmed", "spliced", "halt_accepted")

# Tables that reference securities.id beyond the three this tool re-attributes. A security with
# rows in ANY of these is refused by splice — re-keying scored history is operator surgery, not a
# loader's call. (fundamentals_snapshots included: point-in-time rows must not be silently
# re-attributed either.)
SPLICE_BLOCKING_REFS = (
    ("fundamentals_snapshots", "security_id"),
    ("debates", "security_id"),
    ("agent_proposal_positions", "security_id"),
    ("paper_portfolio_positions", "security_id"),
    ("guardrail_events", "security_id"),
    ("knowledge_base_entries", "security_id"),
    ("evaluation_runs", "benchmark_security_id"),
)


class LoadError(Exception):
    """A failure raised deliberately."""


class FetchError(LoadError):
    """Network failure after retries — maps to EXIT_CONNECTION."""


def connect() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LoadError("DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-delistings")
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute("SET idle_in_transaction_session_timeout = 0")
    return conn


# ── audit ─────────────────────────────────────────────────────────────────────────────────────
# One statement enumerates every internal hole with its missed-covered-sessions count and its
# split-adjusted cross-gap ratio. Used by cmd_audit (which persists it) and exposed as SQL text so
# verify_daily_series.py check 7 can run the identical detection — the tripwire and the audit must
# never disagree about what a hole is.
HOLE_DETECTION_SQL = """
WITH global_gaps AS (
    SELECT c.trade_date FROM market_calendar c
    WHERE c.is_trading_day
      AND c.trade_date BETWEEN (SELECT min(trade_date) FROM price_bars_daily)
                           AND (SELECT max(trade_date) FROM price_bars_daily)
      AND NOT EXISTS (SELECT 1 FROM price_bars_daily d WHERE d.trade_date = c.trade_date)
), r AS (
    SELECT d.security_id, d.trade_date, d.close,
           lag(d.trade_date) OVER w AS prev_d,
           lag(d.close) OVER w AS prev_c
    FROM price_bars_daily d
    WINDOW w AS (PARTITION BY d.security_id ORDER BY d.trade_date)
)
SELECT r.security_id, r.prev_d AS gap_start, r.trade_date AS gap_resume,
       r.prev_c AS close_before, r.close AS close_after,
       (r.trade_date - r.prev_d) AS gap_days,
       (SELECT count(*) FROM market_calendar c
        WHERE c.is_trading_day AND c.trade_date > r.prev_d AND c.trade_date < r.trade_date
          AND c.trade_date NOT IN (SELECT trade_date FROM global_gaps)) AS missed_sessions,
       (r.close / r.prev_c) * COALESCE(
          (SELECT exp(sum(ln(ca.split_ratio))) FROM corporate_actions ca
           WHERE ca.security_id = r.security_id AND ca.action_type = 'split'
             AND ca.ex_date > r.prev_d AND ca.ex_date <= r.trade_date), 1) AS adj_ratio
FROM r
WHERE r.prev_d IS NOT NULL AND r.prev_c > 0
"""


def cmd_audit(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    floor, lo, hi = args.min_missed_sessions, args.ratio_low, args.ratio_high
    with conn.cursor() as cur:
        # f-string interpolates the module-level detection constant, never external input.
        cur.execute(f"CREATE TEMP TABLE tmp_holes AS {HOLE_DETECTION_SQL}")
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE missed_sessions >= %(f)s)                    AS holes,
                   count(*) FILTER (WHERE missed_sessions >= %(f)s
                                      AND (adj_ratio < %(lo)s OR adj_ratio > %(hi)s)) AS out_of_band,
                   count(*) FILTER (WHERE missed_sessions BETWEEN 1 AND %(f)s - 1
                                      AND (adj_ratio < %(lo)s OR adj_ratio > %(hi)s)) AS subfloor_oob
            FROM tmp_holes
            """,
            {"f": floor, "lo": lo, "hi": hi},
        )
        holes, out_of_band, subfloor_oob = cur.fetchone()
    logger.info(
        "audit: %d hole(s) of >= %d missed covered sessions; %d carry a cross-gap ratio outside "
        "[%s, %s] that no recorded action explains", holes, floor, out_of_band, lo, hi,
    )
    # The cohort the floor cannot see — counted every run, never assumed empty. These are holes a
    # routine halt CAN produce, so their out-of-band moves are treated as (usually real) market
    # moves; the count keeps that judgement observable rather than silent.
    logger.info(
        "audit: %d sub-floor hole(s) (1-%d missed sessions) also sit outside the ratio band — "
        "below the classification floor by design (SEC 12(k) suspensions run to 10 sessions), "
        "counted here so the blind spot stays visible", subfloor_oob, floor - 1,
    )
    if args.dry_run:
        logger.info("dry-run: nothing written")
        return EXIT_OK

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, notes) "
            "VALUES ('derived', 'gap_audit', %s, %s) RETURNING id",
            (datetime.now(timezone.utc),
             (f"Gap audit: holes >= {floor} missed covered sessions, ratio band [{lo}, {hi}] "
              f"(B-N2). {holes} holes, {out_of_band} out-of-band, {subfloor_oob} sub-floor "
              "out-of-band (below classification floor, counted).")),
        )
        source_id = cur.fetchone()[0]
        # Disposition merge rules: operator decisions (halt_accepted) and completed splices are
        # never clobbered; provider-derived evidence survives a re-run UNLESS the ratio is now
        # in-band (a since-fetched split explaining the hole downgrades split_missing to
        # halt_consistent, which is the whole point of re-auditing after a fetch).
        cur.execute(
            """
            INSERT INTO price_gap_audit
                (security_id, symbol, gap_start, gap_resume, gap_days, missed_sessions,
                 close_before, close_after, adj_ratio, disposition, evidence, source_id)
            SELECT h.security_id, s.symbol, h.gap_start, h.gap_resume, h.gap_days,
                   h.missed_sessions, h.close_before, h.close_after, round(h.adj_ratio, 8),
                   CASE WHEN h.adj_ratio < %(lo)s OR h.adj_ratio > %(hi)s
                        THEN 'pending_review' ELSE 'halt_consistent' END,
                   format('cross-gap ratio %%s over %%s missed sessions (band [%%s, %%s])',
                          round(h.adj_ratio, 4), h.missed_sessions,
                          %(lo)s::numeric, %(hi)s::numeric),
                   %(src)s
            FROM tmp_holes h JOIN securities s ON s.id = h.security_id
            WHERE h.missed_sessions >= %(f)s
            ON CONFLICT (security_id, gap_start) DO UPDATE SET
                gap_resume      = EXCLUDED.gap_resume,
                gap_days        = EXCLUDED.gap_days,
                missed_sessions = EXCLUDED.missed_sessions,
                close_before    = EXCLUDED.close_before,
                close_after     = EXCLUDED.close_after,
                adj_ratio       = EXCLUDED.adj_ratio,
                source_id       = EXCLUDED.source_id,
                disposition = CASE
                    WHEN price_gap_audit.disposition IN ('spliced', 'halt_accepted')
                        THEN price_gap_audit.disposition
                    WHEN EXCLUDED.disposition = 'halt_consistent'
                         AND price_gap_audit.disposition = 'continuity_confirmed'
                        THEN price_gap_audit.disposition
                    WHEN EXCLUDED.disposition = 'halt_consistent'
                        THEN 'halt_consistent'
                    WHEN price_gap_audit.disposition IN
                         ('identity_break', 'provider_unresolvable', 'split_missing',
                          'continuity_confirmed')
                        THEN price_gap_audit.disposition
                    ELSE EXCLUDED.disposition
                END,
                evidence = CASE
                    WHEN price_gap_audit.disposition IN ('spliced', 'halt_accepted')
                        THEN price_gap_audit.evidence
                    ELSE EXCLUDED.evidence
                END
            """,
            {"f": floor, "lo": lo, "hi": hi, "src": source_id},
        )
        upserted = cur.rowcount
    logger.info("audit: %d hole(s) recorded/refreshed in price_gap_audit", upserted)

    if args.provider:
        rc = _audit_provider_pass(conn, args)
        if rc != EXIT_OK:
            return rc

    with conn.cursor() as cur:
        cur.execute("SELECT disposition, count(*) FROM price_gap_audit GROUP BY 1 ORDER BY 1")
        for disp, n in cur.fetchall():
            logger.info("audit: %-22s %d", disp, n)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM price_gap_audit WHERE disposition = ANY(%s)",
            (list(NON_TERMINAL_DISPOSITIONS),),
        )
        unresolved = cur.fetchone()[0]
    if unresolved:
        logger.warning(
            "%d hole(s) remain unresolved (%s). Resolve with `audit --provider`, "
            "`splice --from-audit`, a `fetch --symbols` for split_missing rows, or an explicit "
            "halt_accepted override. verify_daily_series check 7 FAILS until then.",
            unresolved, ", ".join(NON_TERMINAL_DISPOSITIONS),
        )
    return EXIT_OK


def _audit_provider_pass(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    """Provider-history evidence for pending holes — the check that separated COHR/DBD/VRM/FNGU/
    FIG/AI (identity breaks) from genuine one-issuer moves. yfinance speaks for a symbol's
    CURRENT holder, which is exactly what makes its history's START date evidence: a holder whose
    history begins at our resume date cannot be the issuer of our pre-gap bars."""
    try:
        import yfinance as yf
    except ModuleNotFoundError:
        logger.error("audit --provider needs yfinance — run via bin/db_corporate_actions.sh")
        return EXIT_CONNECTION

    targets = args.dispositions or ["pending_review"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, symbol, gap_start, gap_resume, adj_ratio FROM price_gap_audit "
            "WHERE disposition = ANY(%s) ORDER BY symbol, gap_start",
            (targets,),
        )
        rows = cur.fetchall()
    if not rows:
        logger.info("audit --provider: nothing to check (no %s rows)", "/".join(targets))
        return EXIT_OK
    logger.info("audit --provider: checking %d hole(s) against provider history", len(rows))

    lo, hi = args.ratio_low, args.ratio_high
    consecutive_failures = 0
    checked = 0
    for audit_id, symbol, gap_start, gap_resume, adj_ratio in rows:
        try:
            hist = yf.Ticker(symbol).history(period="max", auto_adjust=False, actions=False)
            dates = [ts.date() for ts in hist.index]
            closes = [float(v) for v in hist["Close"]]
        except Exception as exc:  # noqa: BLE001 — yfinance's failure surface is not a stable public set; every failure is counted, logged, and recorded as provider_unresolvable rather than crashing the audit
            consecutive_failures += 1
            checked += 1
            logger.warning("%s: provider error: %s", symbol, exc)
            if consecutive_failures >= CONSECUTIVE_PROVIDER_FAILURES_ABORT and consecutive_failures == checked:
                logger.error(
                    "first %d provider calls ALL failed — connectivity/wrapper problem, not %d "
                    "coincidentally bad symbols. Aborting; no evidence recorded for them.",
                    consecutive_failures, consecutive_failures,
                )
                return EXIT_CONNECTION
            _set_disposition(conn, audit_id, "provider_unresolvable",
                             f"provider error: {str(exc)[:200]}")
            time.sleep(PROVIDER_CALL_SLEEP_S)
            continue
        consecutive_failures = 0
        checked += 1

        if not dates:
            _set_disposition(conn, audit_id, "provider_unresolvable",
                             "provider returned no history for this symbol")
        else:
            first = dates[0]
            if first > gap_start:
                # The current holder's entire history postdates our pre-gap bars — two issuers.
                _set_disposition(
                    conn, audit_id, "identity_break",
                    f"provider history begins {first}, after our pre-gap bar {gap_start} "
                    f"(resume {gap_resume}) — the current holder cannot be the pre-gap issuer",
                )
            else:
                before = next((c for d, c in zip(reversed(dates), reversed(closes), strict=True)
                               if d <= gap_start and c > 0), None)
                after = next((c for d, c in zip(dates, closes, strict=True)
                              if d >= gap_resume and c > 0), None)
                if before is None or after is None:
                    _set_disposition(conn, audit_id, "provider_unresolvable",
                                     "provider history spans the gap start but has no usable "
                                     "close on both sides of the hole")
                else:
                    # Provider series is on ONE basis (its current one), so its cross-gap ratio
                    # already folds in every action it knows about — no adjustment needed here.
                    their_ratio = after / before
                    if lo <= their_ratio <= hi:
                        _set_disposition(
                            conn, audit_id, "split_missing",
                            f"provider cross-gap ratio {their_ratio:.4f} is in-band while ours "
                            f"is {adj_ratio} — an action inside the hole is unrecorded; run "
                            f"load_corporate_actions.py fetch --symbols {symbol}, adjust, re-audit",
                        )
                    else:
                        _set_disposition(
                            conn, audit_id, "continuity_confirmed",
                            f"provider history spans the hole (begins {first}) and its own "
                            f"cross-gap ratio {their_ratio:.4f} is also outside the band — one "
                            "issuer, real move",
                        )
        time.sleep(PROVIDER_CALL_SLEEP_S)
    logger.info("audit --provider: %d hole(s) checked", checked)
    return EXIT_OK


def _set_disposition(conn: psycopg.Connection, audit_id: int, disposition: str, evidence: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE price_gap_audit SET disposition = %s, evidence = %s WHERE id = %s",
            (disposition, evidence, audit_id),
        )
    logger.info("audit id=%d -> %s", audit_id, disposition)


# ── splice ────────────────────────────────────────────────────────────────────────────────────
def find_latest_gaps(conn: psycopg.Connection, min_gap_days: int) -> list[tuple[int, str, date, date]]:
    """(security_id, symbol, last_bar_before_gap, first_bar_after_gap) for each security's LATEST
    over-threshold hole. Latest-first per security so iterated passes peel identities newest-out."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH g AS (
                SELECT d.security_id, d.trade_date,
                       lag(d.trade_date) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev
                FROM price_bars_daily d
            ), latest AS (
                SELECT DISTINCT ON (security_id) security_id, prev, trade_date
                FROM g
                WHERE prev IS NOT NULL AND trade_date - prev > %s
                ORDER BY security_id, trade_date DESC
            )
            SELECT l.security_id, s.symbol, l.prev, l.trade_date
            FROM latest l JOIN securities s ON s.id = l.security_id
            ORDER BY s.symbol
            """,
            (min_gap_days,),
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def _splice_one(conn: psycopg.Connection, sec_id: int, symbol: str,
                gap_start: date, resume: date, source_id: int) -> int:
    """Split one security at its gap. Returns the new (post-gap) security id.

    One transaction per security: the delist, the new row, and every moved child row commit
    together or not at all — a half-spliced identity would be worse than a spliced one.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT delisted_at, source_id FROM securities WHERE id = %s FOR UPDATE", (sec_id,))
        old_delisted, _old_source = cur.fetchone()

        for table, col in SPLICE_BLOCKING_REFS:
            # Identifiers come from the hardcoded tuple above, never external input.
            cur.execute(f"SELECT count(*) FROM {table} WHERE {col} = %s", (sec_id,))
            if cur.fetchone()[0]:
                raise LoadError(
                    f"{symbol} (id={sec_id}): {table} rows reference this security — splicing "
                    "would re-attribute recorded history. Resolve by hand first."
                )

        # The pre-gap issuer dies at the gap. Set BEFORE inserting the successor, or the partial
        # unique index (one LIVE holder per symbol) rejects the insert.
        cur.execute(
            "UPDATE securities SET delisted_at = %s WHERE id = %s",
            (gap_start + timedelta(days=1), sec_id),
        )
        # The post-gap issuer: first_seen is its genuine reappearance date; it inherits whatever
        # delisted status the combined row had (NULL if the symbol trades through archive end).
        cur.execute(
            "INSERT INTO securities (symbol, first_seen, delisted_at, source_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (symbol, resume, old_delisted, source_id),
        )
        new_id = cur.fetchone()[0]

        cur.execute(
            "UPDATE price_bars_daily SET security_id = %s WHERE security_id = %s AND trade_date >= %s",
            (new_id, sec_id, resume),
        )
        moved_daily = cur.rowcount
        cur.execute(
            "UPDATE price_bars_minute SET security_id = %s WHERE security_id = %s AND ts >= %s",
            (new_id, sec_id,
             datetime.combine(resume, datetime.min.time(), tzinfo=timezone.utc)),
        )
        # ALL corporate actions move to the post-gap (current) identity: they were fetched from
        # the provider BY SYMBOL, so they describe the current holder — leaving any on the dead
        # issuer is the exact wrong-adjustment bug being repaired.
        cur.execute("UPDATE corporate_actions SET security_id = %s WHERE security_id = %s", (new_id, sec_id))
        moved_actions = cur.rowcount

    logger.info(
        "spliced %-8s id=%d → new id=%d at gap %s → %s (%d daily bars, %d actions moved)",
        symbol, sec_id, new_id, gap_start, resume, moved_daily, moved_actions,
    )
    return new_id


def cmd_splice(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    if args.from_audit:
        return _splice_from_audit(conn, args)
    return _splice_by_threshold(conn, args)


def _splice_from_audit(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    """Splice the holes the audit classified as identity breaks (or could not resolve).

    provider_unresolvable defaults to splice: the fabricated cross-gap "return" is the number
    B-S5/B-N2 exist to kill, and splicing a continuous-but-unprovable issuer merely truncates a
    series at a void no backtest could honestly price across. --include-pending extends this to
    rows with no provider evidence at all, for environments without egress — loudly.
    """
    dispositions = ["identity_break", "provider_unresolvable"]
    if args.include_pending:
        logger.warning(
            "--include-pending: splicing pending_review holes WITHOUT provider evidence — "
            "price-discontinuity evidence only. Recorded as such in each audit row."
        )
        dispositions.append("pending_review")
    with conn.cursor() as cur:
        # Latest hole first per security: peeling newest-out keeps earlier holes' bar ranges
        # attached to the original id, and corporate actions land on the newest identity (the
        # current holder) on the first splice.
        cur.execute(
            "SELECT a.id, a.security_id, a.symbol, a.gap_start, a.gap_resume, a.disposition "
            "FROM price_gap_audit a WHERE a.disposition = ANY(%s) "
            "ORDER BY a.security_id, a.gap_start DESC",
            (dispositions,),
        )
        rows = cur.fetchall()
    if not rows:
        logger.info("splice --from-audit: no holes with disposition in %s", dispositions)
        return EXIT_OK
    logger.info("splice --from-audit: %d hole(s) to splice (%s)", len(rows), "/".join(dispositions))
    if args.dry_run:
        for _aid, sec_id, symbol, gap_start, gap_resume, disp in rows[:50]:
            logger.info("  would splice %-8s id=%d at %s -> %s (%s)",
                        symbol, sec_id, gap_start, gap_resume, disp)
        if len(rows) > 50:
            logger.info("  ... and %d more", len(rows) - 50)
        logger.info("dry-run: nothing written")
        return EXIT_OK

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, notes) "
            "VALUES ('derived', 'ticker_splice', %s, %s) RETURNING id",
            (datetime.now(timezone.utc),
             ("Audit-driven splice (B-N2): identities split at holes classified "
              f"{'/'.join(dispositions)} in price_gap_audit.")),
        )
        source_id = cur.fetchone()[0]

    total = 0
    skipped: list[str] = []
    for audit_id, sec_id, symbol, gap_start, gap_resume, disp in rows:
        with conn.cursor() as cur:
            # The hole must still exist as recorded — both boundary bars owned by this identity.
            # (A hole already spliced by an earlier pass no longer is; skip it loudly.)
            cur.execute(
                "SELECT bool_or(trade_date = %s), bool_or(trade_date = %s) FROM price_bars_daily "
                "WHERE security_id = %s AND trade_date IN (%s, %s)",
                (gap_start, gap_resume, sec_id, gap_start, gap_resume),
            )
            has_start, has_resume = cur.fetchone()
        if not (has_start and has_resume):
            logger.warning(
                "SKIP %s (audit id=%d): the recorded hole %s -> %s no longer belongs to "
                "security id=%d — already spliced or reloaded; re-run `audit` to refresh",
                symbol, audit_id, gap_start, gap_resume, sec_id,
            )
            skipped.append(symbol)
            continue
        try:
            _splice_one(conn, sec_id, symbol, gap_start, gap_resume, source_id)
        except LoadError as exc:
            logger.error("SKIP %s", exc)
            skipped.append(symbol)
            continue
        _set_disposition(
            conn, audit_id, "spliced",
            f"spliced (was {disp}): pre-gap identity delisted at {gap_start}, post-gap bars and "
            "all corporate actions moved to a new identity",
        )
        total += 1

    logger.info("splice --from-audit complete — %d identit(y/ies) split, %d skipped", total, len(skipped))
    if total:
        logger.warning("factors are now STALE for the split identities — "
                       "re-run: bin/db_corporate_actions.sh adjust")
    return EXIT_VALIDATION if skipped else EXIT_OK


def _splice_by_threshold(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    total = 0
    skipped: list[str] = []
    # Iterate: peeling the latest segment off can expose an earlier over-threshold hole in what
    # remains (a thrice-recycled ticker). Bounded — each pass strictly reduces remaining holes.
    for round_no in range(1, 20):
        gaps = find_latest_gaps(conn, args.min_gap_days)
        gaps = [g for g in gaps if g[1] not in skipped]
        if not gaps:
            break
        logger.info("pass %d: %d securit(y/ies) with an internal hole > %d days",
                    round_no, len(gaps), args.min_gap_days)
        if args.dry_run:
            for sec_id, symbol, prev, nxt in gaps[:50]:
                logger.info("  would splice %-8s id=%d at %s → %s (%d days)",
                            symbol, sec_id, prev, nxt, (nxt - prev).days)
            if len(gaps) > 50:
                logger.info("  … and %d more", len(gaps) - 50)
            logger.info("dry-run: nothing written")
            return EXIT_OK

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO data_sources (provider, dataset, fetched_at, notes) "
                "VALUES ('derived', 'ticker_splice', %s, %s) RETURNING id",
                (datetime.now(timezone.utc),
                 (f"Recycled-ticker splice pass {round_no}: identities split at internal holes "
                  f"> {args.min_gap_days} days (semantics review B-S5).")),
            )
            source_id = cur.fetchone()[0]

        for sec_id, symbol, prev, nxt in gaps:
            try:
                _splice_one(conn, sec_id, symbol, prev, nxt, source_id)
                total += 1
            except LoadError as exc:
                logger.error("SKIP %s", exc)
                skipped.append(symbol)
    else:
        remaining = [g for g in find_latest_gaps(conn, args.min_gap_days) if g[1] not in skipped]
        if remaining:  # pragma: no cover — 20 nested recyclings of one ticker does not exist
            logger.error("pass budget exhausted with %d hole(s) remaining — investigate", len(remaining))
            return EXIT_VALIDATION

    logger.info("splice complete — %d identit(y/ies) split, %d skipped", total, len(skipped))
    if total:
        logger.warning("factors are now STALE for the split identities — "
                       "re-run: bin/db_corporate_actions.sh adjust")
    return EXIT_VALIDATION if skipped else EXIT_OK


# ── infer ─────────────────────────────────────────────────────────────────────────────────────
def cmd_infer(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM price_bars_daily")
        archive_end = cur.fetchone()[0]
    if archive_end is None:
        raise LoadError("price_bars_daily is empty — nothing to infer from")

    # The cutoff is N trading sessions before the archive's end, from the calendar — absence is
    # only meaningful in sessions the market actually held.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date FROM market_calendar WHERE is_trading_day AND trade_date <= %s "
            "ORDER BY trade_date DESC OFFSET %s LIMIT 1",
            (archive_end, args.confirm_sessions),
        )
        row = cur.fetchone()
    if row is None:
        raise LoadError("market_calendar has no coverage before the archive end — load the calendar first")
    cutoff = row[0]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM securities s
            JOIN (SELECT security_id, max(trade_date) AS last_bar
                  FROM price_bars_daily GROUP BY security_id) lb ON lb.security_id = s.id
            WHERE s.delisted_at IS NULL AND lb.last_bar < %s
            """,
            (cutoff,),
        )
        n_dead = cur.fetchone()[0]
    logger.info(
        "%d live securit(y/ies) have no bar in the final %d sessions (cutoff %s, archive end %s)",
        n_dead, args.confirm_sessions, cutoff, archive_end,
    )
    if args.dry_run:
        logger.info("dry-run: nothing written")
        return EXIT_OK
    if not n_dead:
        return EXIT_OK

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, row_count, notes) "
            "VALUES ('derived', 'delisting_inference', %s, %s, %s) RETURNING id",
            (datetime.now(timezone.utc), n_dead,
             (f"Absence-based delisting: last bar more than {args.confirm_sessions} sessions "
              f"before archive end {archive_end}. delisted_at = last bar + 1 day (inference, "
              "not an authoritative date; the fmp command refines where FMP knows better).")),
        )
        source_id = cur.fetchone()[0]
        cur.execute(
            """
            UPDATE securities s
            SET delisted_at = lb.last_bar + 1, source_id = COALESCE(s.source_id, %s)
            FROM (SELECT security_id, max(trade_date) AS last_bar
                  FROM price_bars_daily GROUP BY security_id) lb
            WHERE lb.security_id = s.id AND s.delisted_at IS NULL AND lb.last_bar < %s
            """,
            (source_id, cutoff),
        )
        marked = cur.rowcount
    logger.info("marked %d securit(y/ies) delisted by absence", marked)
    return EXIT_OK


# ── fmp ───────────────────────────────────────────────────────────────────────────────────────
class FmpTierLimit(LoadError):
    """FMP refused a page with a payment/permission status — a tier boundary, not an outage.

    Observed live (2026-07-29): the free tier serves page 0 of delisted-companies and returns
    HTTP 402 for page >= 1. That is not transient, must not be retried, and — when at least one
    page succeeded — must not fail the run: partial recent coverage is real data.
    """


def fetch_fmp_page(page: int, api_key: str) -> list[dict]:
    url = f"{FMP_URL}?page={page}&limit={FMP_PAGE_SIZE}&apikey={api_key}"
    last_exc: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            # Constant https host; the only variable parts are an int and the operator's own key.
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and "Error Message" in data:
                raise LoadError(f"FMP refused page {page}: {data['Error Message'][:200]}")
            if not isinstance(data, list):
                raise LoadError(f"FMP page {page}: unexpected payload shape {type(data).__name__}")
            return data
        except urllib.error.HTTPError as exc:
            # 4xx is a decision, not weather: retrying cannot change it. 402/403 = tier boundary.
            if exc.code in (402, 403):
                raise FmpTierLimit(f"FMP page {page}: HTTP {exc.code} — beyond this key's tier") from exc
            if 400 <= exc.code < 500:
                raise LoadError(f"FMP page {page}: HTTP {exc.code}") from exc
            last_exc = exc  # 5xx: transient, fall through to retry
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
        if attempt < FETCH_ATTEMPTS:
            delay = FETCH_BACKOFF_BASE_S * (2 ** (attempt - 1)) * (0.5 + random.random())
            logger.warning("FMP page %d attempt %d/%d failed (%s) — retrying in %.1fs",
                           page, attempt, FETCH_ATTEMPTS, last_exc, delay)
            time.sleep(delay)
    raise FetchError(f"FMP fetch failed after {FETCH_ATTEMPTS} attempts: {last_exc}")


def cmd_fmp(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise LoadError("FMP_API_KEY is not set — export it (it lives in backend/.env) or skip this command")

    with conn.cursor() as cur:
        cur.execute("SELECT min(trade_date), max(trade_date) FROM price_bars_daily")
        archive_start, _archive_end = cur.fetchone()
    if archive_start is None:
        raise LoadError("price_bars_daily is empty — nothing to match against")

    # Newest-first pages; stop once a whole page predates the archive (nothing older can match).
    entries: list[tuple[str, date]] = []
    bad_symbols = 0
    pages_fetched = 0
    for page in range(args.max_pages):
        try:
            data = fetch_fmp_page(page, api_key)
        except FmpTierLimit as exc:
            if page == 0:
                raise  # even page 0 refused: the key/tier is unusable, fail loudly
            logger.warning(
                "%s — the free tier serves only the first %d page(s). Proceeding with the "
                "%d recent record(s) fetched; historical delisting DATES stay inference-based "
                "(the `infer` command) until a deeper feed is available.",
                exc, page, len(entries),
            )
            break
        pages_fetched += 1
        if not data:
            break
        page_dates: list[date] = []
        for rec in data:
            sym_raw = (rec.get("symbol") or "").strip()
            d_raw = (rec.get("delistedDate") or "").strip()
            try:
                d = date.fromisoformat(d_raw)
            except ValueError:
                bad_symbols += 1
                continue
            page_dates.append(d)
            sym = sym_raw.replace("-", ".")
            if not SYMBOL_RE.match(sym):
                bad_symbols += 1
                continue
            entries.append((sym, d))
        if page_dates and max(page_dates) < archive_start:
            logger.info("page %d entirely predates the archive (%s) — stopping", page, archive_start)
            break
    else:
        logger.warning("hit --max-pages=%d before exhausting FMP's list — older delistings not fetched",
                       args.max_pages)

    logger.info("FMP: %d pages, %d usable delisting records, %d unusable (bad symbol/date)",
                pages_fetched, len(entries), bad_symbols)
    if not entries:
        raise LoadError("FMP returned no usable delisting records")

    if args.dry_run:
        logger.info("dry-run: nothing written")
        return EXIT_OK

    matched = 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, source_uri, notes) "
            "VALUES ('fmp', 'delistings', %s, %s, %s) RETURNING id",
            (datetime.now(timezone.utc), FMP_URL,
             (f"FMP delisted-companies, {pages_fetched} pages / {len(entries)} records. Matched "
              "to identities whose last bar falls in [delistedDate-60d, delistedDate) — the "
              "window is what keeps a recycled ticker's old delisting off its current holder.")),
        )
        source_id = cur.fetchone()[0]
        # The per-security last bar ONCE into a temp table — recomputing a 12.8M-row aggregate
        # inside every per-record UPDATE turned 99 records into ~15 minutes (observed live).
        cur.execute(
            "CREATE TEMP TABLE tmp_last_bar ON COMMIT DROP AS "
            "SELECT security_id, max(trade_date) AS last_bar FROM price_bars_daily GROUP BY security_id"
        )
        cur.execute("CREATE INDEX ON tmp_last_bar (security_id)")
        for sym, d in entries:
            cur.execute(
                """
                UPDATE securities s
                SET delisted_at = %(d)s
                FROM tmp_last_bar lb
                WHERE s.id = lb.security_id AND s.symbol = %(sym)s
                  AND lb.last_bar < %(d)s AND lb.last_bar >= %(d)s - INTERVAL '60 days'
                  AND (s.delisted_at IS NULL OR s.delisted_at <> %(d)s)
                """,
                {"sym": sym, "d": d},
            )
            if cur.rowcount:
                matched += 1
        cur.execute("UPDATE data_sources SET row_count = %s WHERE id = %s", (matched, source_id))
    logger.info("FMP: %d identit(y/ies) dated authoritatively", matched)
    return EXIT_OK


# ── entry point ───────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_delistings")
    p.add_argument("command", choices=("audit", "splice", "infer", "fmp"))
    p.add_argument("--min-missed-sessions", type=int, default=AUDIT_MIN_MISSED_SESSIONS,
                   help="audit: covered-session floor for classification (default "
                        f"{AUDIT_MIN_MISSED_SESSIONS} = SEC 12(k) suspension ceiling)")
    p.add_argument("--ratio-low", type=float, default=AUDIT_RATIO_LOW,
                   help=f"audit: lower edge of the explicable cross-gap band (default {AUDIT_RATIO_LOW})")
    p.add_argument("--ratio-high", type=float, default=AUDIT_RATIO_HIGH,
                   help=f"audit: upper edge of the explicable cross-gap band (default {AUDIT_RATIO_HIGH})")
    p.add_argument("--provider", action="store_true",
                   help="audit: gather provider-history evidence for pending holes (egress required)")
    p.add_argument("--dispositions", nargs="*",
                   help="audit --provider: which dispositions to (re)check (default pending_review)")
    p.add_argument("--from-audit", action="store_true",
                   help="splice: splice holes classified identity_break/provider_unresolvable "
                        "in price_gap_audit (the normal mode)")
    p.add_argument("--include-pending", action="store_true",
                   help="splice --from-audit: also splice pending_review holes (no-egress "
                        "environments; price evidence only — loud)")
    p.add_argument("--min-gap-days", type=int, default=180,
                   help="splice (threshold mode, superseded by --from-audit): hole length in "
                        "calendar days (default 180)")
    p.add_argument("--confirm-sessions", type=int, default=5,
                   help="infer: sessions of absence before the archive end that mean dead (default 5)")
    p.add_argument("--max-pages", type=int, default=120,
                   help="fmp: page budget (100 records/page; free tier allows 250 calls/day)")
    p.add_argument("--dry-run", action="store_true")
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
        return {"audit": cmd_audit, "splice": cmd_splice, "infer": cmd_infer,
                "fmp": cmd_fmp}[args.command](conn, args)
    except FetchError as exc:
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
