#!/usr/bin/env python3
"""Load recent daily bars from FMP into price_bars_daily.

WHY A SECOND BAR LOADER
    load_daily_bars.py derives bars from a local Polygon minute archive. That archive is static and
    ends 2025-10-02, so nothing in the database knows about any session since. The marking job
    correctly refuses to value a portfolio on a day it has no bar for, which means the equity curve
    cannot start until fresh bars exist. This is the loader that keeps them current.

THE CLOSE MEANS SOMETHING DIFFERENT HERE, AND THAT MATTERS
    load_daily_bars.py's docstring is explicit: its close is the last regular-session MINUTE close
    (15:59), not the official closing-auction print, because the auction cannot be recovered from a
    1-minute archive. FMP reports the OFFICIAL close.

    So bars before 2025-10-03 and bars after it are not the same measurement. On most days the gap
    is pennies; on a volatile close or an index rebalance it is not. Two consequences, both stated
    rather than papered over:

      * A return computed ACROSS the join date has one leg of each kind. Single-day noise, but it is
        real and it is at a known date.
      * Every row this loader writes carries its own data_sources row naming FMP, so the provenance
        of any bar is answerable rather than assumed.

    The right long-term fix is one source for the whole series. Until then the discontinuity is
    documented, dated, and traceable — which is the difference between a known limitation and a
    silent error.

ADJUSTED CLOSE
    FMP's `adjClose` is split- AND dividend-adjusted, and it is BACKWARD-looking: the whole history
    shifts when a new corporate action lands. It is stored, but `close` stays the raw print, because
    the marking job values shares held at the price they actually traded.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from src.fmp import get_shared_client, to_fmp_symbol

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("load_daily_bars_fmp")

EXIT_OK, EXIT_FAIL = 0, 1
PROVIDER = "fmp"
DATASET = "price_bars_daily"


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="load_daily_bars_fmp")
    ap.add_argument("--symbols", default="", help="comma-separated; default: every held name")
    ap.add_argument("--from", dest="from_date", default=None, help="YYYY-MM-DD (default: continue from the last stored bar)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    dsn = __import__("os").environ.get("DATABASE_URL", "")
    client = get_shared_client()
    today = datetime.now(timezone.utc).date()

    with psycopg.connect(dsn) as conn:
        if args.symbols.strip():
            wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            wanted = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT s.symbol FROM paper_portfolio_positions p"
                    " JOIN securities s ON s.id = p.security_id"
                    " JOIN paper_portfolios pp ON pp.id = p.portfolio_id"
                    " WHERE pp.kind = 'real' AND p.exit_date IS NULL ORDER BY 1"
                ).fetchall()
            ]
        if not wanted:
            logger.error("no symbols to load")
            return EXIT_FAIL

        total_rows = 0
        for symbol in wanted:
            sec = conn.execute(
                "SELECT id FROM securities WHERE upper(symbol)=upper(%s)", (symbol,)
            ).fetchone()
            if sec is None:
                logger.warning("%s is not in `securities` — skipped", symbol)
                continue
            security_id = sec[0]

            if args.from_date:
                start = date.fromisoformat(args.from_date)
            else:
                last = conn.execute(
                    "SELECT max(trade_date) FROM price_bars_daily WHERE security_id=%s",
                    (security_id,),
                ).fetchone()[0]
                start = (last + timedelta(days=1)) if last else today - timedelta(days=400)

            if start > today:
                logger.info("%-6s already current (last bar %s)", symbol, start - timedelta(days=1))
                continue

            try:
                rows = client.get(
                    "historical-price-eod/full",
                    {"symbol": to_fmp_symbol(symbol), "from": start.isoformat(), "to": today.isoformat()},
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("%s: FMP fetch failed: %s", symbol, exc)
                continue
            if not isinstance(rows, list) or not rows:
                logger.info("%-6s no new bars from %s", symbol, start)
                continue

            bars = []
            for r in rows:
                try:
                    d = date.fromisoformat(str(r.get("date"))[:10])
                except (TypeError, ValueError):
                    continue
                close = _num(r.get("close"))
                if close is None:
                    continue
                bars.append((
                    security_id, d, _num(r.get("open")), _num(r.get("high")),
                    _num(r.get("low")), close, _num(r.get("adjClose")),
                    int(r.get("volume") or 0),
                ))
            if not bars:
                continue

            if args.dry_run:
                logger.info("DRY RUN %-6s %d bar(s) %s..%s", symbol, len(bars),
                            min(b[1] for b in bars), max(b[1] for b in bars))
                total_rows += len(bars)
                continue

            # Provenance and rows in ONE transaction, matching every other loader here: a
            # data_sources row claiming bars that never landed is a record asserting something false.
            note = (
                f"{symbol}: official closing prints from FMP. NOTE that archive-derived bars "
                f"before 2025-10-03 use the 15:59 minute close rather than the auction print, so "
                f"a return spanning that date has one leg of each kind."
            )
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO data_sources (provider, dataset, fetched_at, period_start,"
                    " period_end, source_uri, row_count, notes)"
                    " VALUES (%s,%s,now(),%s,%s,%s,%s,%s) RETURNING id",
                    (PROVIDER, DATASET, min(b[1] for b in bars), max(b[1] for b in bars),
                     "https://financialmodelingprep.com/stable/historical-price-eod/full",
                     len(bars), note),
                )
                source_id = cur.fetchone()[0]
                cur.executemany(
                    "INSERT INTO price_bars_daily (security_id, trade_date, open, high, low,"
                    " close, adj_close, volume, source_id)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (security_id, trade_date) DO NOTHING",
                    [(*b, source_id) for b in bars],
                )
            logger.info("%-6s +%d bar(s) through %s", symbol, len(bars), max(b[1] for b in bars))
            total_rows += len(bars)

    logger.info("done: %d bar(s) across %d symbol(s)", total_rows, len(wanted))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
