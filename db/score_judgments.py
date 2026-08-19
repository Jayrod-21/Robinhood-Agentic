#!/usr/bin/env python3
"""Score judgments whose horizon has elapsed: what happened after each call.

THE RULE, STATED ONCE
    /api/calibration declares the outcome definition — "counterfactual return positive over 5
    trading days" — and this implements exactly that rather than inventing a second one:

        buy   correct when the name ROSE over the window
        hold  correct when the name ROSE   (holding was the right call if it went up)
        sell  correct when the name FELL   (selling was right if it went down)
        escalate  not scored at all — declining to make a directional call is not a call, and
                  grading it either way would invent an opinion the juror explicitly withheld

WHY TRADING DAYS AND NOT CALENDAR DAYS
    Five calendar days from a Thursday is the following Tuesday, and includes a weekend the market
    was shut for. market_calendar is the authority; a call made on a Friday is graded against the
    fifth SESSION after it.

WHAT IT REFUSES TO DO
    Score a window that has not finished, or one where a bar is missing. Both would produce a
    number, and a number here becomes part of an agent's permanent track record. An unscored
    judgment is simply absent from calibration, which the contract treats as excluded rather than
    wrong — the honest treatment for an outcome nobody knows yet.

Exit codes: 0 ok · 1 validation · 2 SQL failure · 3 connection.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("score_judgments")

EXIT_OK, EXIT_VALIDATION, EXIT_SQL, EXIT_CONNECTION = 0, 1, 2, 3

DEFAULT_HORIZON = 5
SCORING_BASIS = "counterfactual_return_positive_v1"

# Judgments with a horizon that has fully elapsed and both bars present.
#
# The decision date is the debate's session; the outcome date is the Nth trading session after it.
# Both prices come from price_bars_daily.close — the raw print, matching what the marking job values
# positions at, rather than adj_close which shifts under corporate actions and would silently
# restate a past call's outcome.
CANDIDATES = """
WITH decided AS (
    SELECT j.id                AS judgment_id,
           j.decision,
           d.security_id,
           (d.started_at AT TIME ZONE 'America/New_York')::date AS decision_date
      FROM judgments j
      JOIN debates d ON d.id = j.debate_id
     WHERE j.decision <> 'escalate'
       AND d.security_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM judgment_outcomes o WHERE o.judgment_id = j.id)
),
anchored AS (
    SELECT dd.*,
           -- The session ON or BEFORE the decision date: a call made after the close, or on a
           -- holiday, is anchored to the last session that actually traded.
           (SELECT max(c.trade_date) FROM market_calendar c
             WHERE c.is_trading_day AND c.trade_date <= dd.decision_date) AS entry_date
      FROM decided dd
),
windowed AS (
    SELECT a.*,
           (SELECT c.trade_date FROM market_calendar c
             WHERE c.is_trading_day AND c.trade_date > a.entry_date
             ORDER BY c.trade_date OFFSET %(horizon)s - 1 LIMIT 1) AS exit_date
      FROM anchored a
     WHERE a.entry_date IS NOT NULL
)
SELECT w.judgment_id, w.decision, w.entry_date, w.exit_date,
       be.close AS entry_price, bx.close AS exit_price
  FROM windowed w
  JOIN price_bars_daily be ON be.security_id = w.security_id AND be.trade_date = w.entry_date
  JOIN price_bars_daily bx ON bx.security_id = w.security_id AND bx.trade_date = w.exit_date
 WHERE w.exit_date IS NOT NULL
   AND bx.close > 0 AND be.close > 0
"""


def is_correct(decision: str, forward_return: float) -> bool:
    """The rule from the module docstring, in one place."""
    if decision == "sell":
        return forward_return < 0
    return forward_return > 0          # buy and hold both want the name up


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="trading days")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescore", action="store_true",
                    help="recompute rows that already exist (use after a rule change)")
    args = ap.parse_args(argv)

    if args.horizon < 1:
        logger.error("--horizon must be at least 1 trading day")
        return EXIT_VALIDATION

    sql = CANDIDATES
    if args.rescore:
        sql = sql.replace(
            "       AND NOT EXISTS (SELECT 1 FROM judgment_outcomes o WHERE o.judgment_id = j.id)\n", "")

    dsn = os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg.connect(dsn) if dsn else psycopg.connect()
    except psycopg.OperationalError as exc:
        logger.error("cannot connect: %s", exc)
        return EXIT_CONNECTION

    with conn:
        try:
            rows = conn.execute(sql, {"horizon": args.horizon}).fetchall()
        except psycopg.Error as exc:
            logger.error("candidate query failed: %s", exc)
            return EXIT_SQL

        if not rows:
            logger.info("nothing to score: no judgment has a fully elapsed %d-session window with "
                        "both bars present", args.horizon)
            return EXIT_OK

        scored = []
        for judgment_id, decision, entry_date, exit_date, entry_price, exit_price in rows:
            forward = (float(exit_price) - float(entry_price)) / float(entry_price)
            scored.append((judgment_id, args.horizon, entry_date, exit_date,
                           entry_price, exit_price, round(forward, 8),
                           is_correct(decision, forward), SCORING_BASIS))

        hits = sum(1 for r in scored if r[7])
        logger.info("%d judgment(s) scorable: %d correct, %d not (%.0f%%)",
                    len(scored), hits, len(scored) - hits, 100.0 * hits / len(scored))

        if args.dry_run:
            logger.info("DRY RUN — nothing written")
            return EXIT_OK

        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO judgment_outcomes (judgment_id, horizon_days, decision_date,"
                    " outcome_date, entry_price, exit_price, forward_return, is_correct,"
                    " scoring_basis) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (judgment_id) DO UPDATE SET"
                    "   horizon_days=EXCLUDED.horizon_days, decision_date=EXCLUDED.decision_date,"
                    "   outcome_date=EXCLUDED.outcome_date, entry_price=EXCLUDED.entry_price,"
                    "   exit_price=EXCLUDED.exit_price, forward_return=EXCLUDED.forward_return,"
                    "   is_correct=EXCLUDED.is_correct, scoring_basis=EXCLUDED.scoring_basis,"
                    "   scored_at=now()",
                    scored,
                )
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            logger.error("write failed: %s", exc)
            return EXIT_SQL

    logger.info("wrote %d outcome(s) at a %d-session horizon (%s)",
                len(scored), args.horizon, SCORING_BASIS)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
