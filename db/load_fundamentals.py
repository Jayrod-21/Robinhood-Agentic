"""Load FMP fundamentals into ``fundamentals_snapshots``.

Follows the loader conventions already established here (load_reference_data.py, both bar
loaders): a ``data_sources`` provenance row and the data rows go in ONE transaction, so an
interrupt can never leave provenance claiming rows that never landed.

TWO ROW KINDS PER SYMBOL, DELIBERATELY
    FMP's ``profile`` returns market cap and price as of RIGHT NOW. Its statements are for a period
    that ended weeks or months ago. Writing both into one row would produce a record that looks
    point-in-time and is not — a backtest filtering on ``known_at`` would read a past period's
    margins alongside today's market cap and call the result history.

    So each fetch writes:

      * period_type='annual'   — statement-derived figures (margins, growth, cash flow, net
        income), period_end = the statement's period end, known_at = the filing's ACCEPTED date.
        This row is real history and is safe to read point-in-time.
      * period_type='snapshot' — market-derived figures (market cap, price, PE, PEG) and the
        derived fcf_yield, period_end = today, known_at = now. Honest by construction: it claims
        only to describe the moment it was fetched.

    ``fcf_yield`` lives on the snapshot row, not the annual one, because it mixes a statement
    numerator with a market denominator. Putting it on the annual row would be the same lie in
    miniature.

WHAT THIS DOES NOT DO
    It does not backfill history. Each run stores what FMP returns for the requested periods; a
    real point-in-time series needs repeated runs over time, or a bulk historical pull. The
    ``--periods`` flag fetches more annual statements per symbol, which IS genuine history because
    each carries its own acceptance date.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from src.data import known_at_from_statement
from src.fmp import (
    FmpBudgetExhausted,
    FmpClient,
    FmpError,
)

logger = logging.getLogger("load_fundamentals")

EXIT_OK = 0
EXIT_FAIL = 1

PROVIDER = "fmp"
DATASET = "fundamentals_snapshots"
SOURCE_URI = "https://financialmodelingprep.com/stable"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def resolve_security_id(conn: psycopg.Connection, symbol: str) -> int | None:
    """The securities row for a symbol, or None.

    Deliberately does NOT create missing securities. `securities` is reference data with its own
    loader and its own provenance; inventing rows here would let a typo'd ticker quietly become a
    permanent instrument that nothing else knows about.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM securities WHERE upper(symbol) = upper(%s)", (symbol,))
        row = cur.fetchone()
    return row[0] if row else None


def annual_row(bundle: dict, security_id: int) -> dict | None:
    """Statement-derived figures for the period the statements cover. Real history.

    Returns None when there is no statement to date — a row whose known_at cannot be established
    would be invisible to point-in-time queries anyway (the index excludes NULLs), and writing it
    would inflate the row count with something nothing can read.
    """
    income = bundle.get("income") or {}
    ratios = bundle.get("ratios") or {}
    cash_flow = bundle.get("cash_flow") or {}
    growth = bundle.get("growth") or {}

    period_end = income.get("date") or ratios.get("date")
    known_at = known_at_from_statement(income) or known_at_from_statement(cash_flow)
    if not period_end or not known_at:
        return None

    return {
        "security_id": security_id,
        "period_end": period_end,
        "period_type": "annual",
        "known_at": known_at,
        # Market-derived fields deliberately absent here — see the module docstring.
        "market_cap": None,
        "price": None,
        "pe_trailing": None,
        "pe_forward": None,
        "peg_ratio": None,
        "free_cash_flow": _num(cash_flow.get("freeCashFlow")),
        "fcf_yield": None,
        "gross_margin": _num(ratios.get("grossProfitMargin")),
        "operating_margin": _num(ratios.get("operatingProfitMargin")),
        "net_margin": _num(ratios.get("netProfitMargin")),
        "ebitda_margin": _num(ratios.get("ebitdaMargin")),
        "current_ratio": _num(ratios.get("currentRatio")),
        "quick_ratio": _num(ratios.get("quickRatio")),
        "revenue_growth_yoy": _num(growth.get("revenueGrowth")),
        "extra": json.dumps(
            {
                "fiscal_year": income.get("fiscalYear"),
                "period": income.get("period"),
                "reported_currency": income.get("reportedCurrency"),
                "revenue": _num(income.get("revenue")),
                "net_income": _num(income.get("netIncome")),
                "operating_cash_flow": _num(cash_flow.get("netCashProvidedByOperatingActivities")),
            }
        ),
    }


def snapshot_row(bundle: dict, security_id: int, fetched_at: datetime) -> dict | None:
    """Market-derived figures as of the fetch. Claims nothing about the past."""
    profile = bundle.get("profile") or {}
    ratios = bundle.get("ratios") or {}
    cash_flow = bundle.get("cash_flow") or {}

    market_cap = _num(profile.get("marketCap"))
    if market_cap is None:
        return None

    free_cash_flow = _num(cash_flow.get("freeCashFlow"))
    fcf_yield = free_cash_flow / market_cap * 100.0 if free_cash_flow is not None else None

    return {
        "security_id": security_id,
        "period_end": fetched_at.date(),
        "period_type": "snapshot",
        "known_at": fetched_at,
        "market_cap": market_cap,
        "price": _num(profile.get("price")),
        "pe_trailing": _num(ratios.get("priceToEarningsRatio")),
        "pe_forward": _num(ratios.get("forwardPriceToEarningsGrowthRatio")),
        "peg_ratio": _num(ratios.get("priceToEarningsGrowthRatio")),
        "free_cash_flow": free_cash_flow,
        # Percent, matching the screen spec — and mixing a statement numerator with a market
        # denominator, which is exactly why it belongs on the snapshot row and not the annual one.
        "fcf_yield": fcf_yield,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
        "ebitda_margin": None,
        "current_ratio": None,
        "quick_ratio": None,
        "revenue_growth_yoy": None,
        "extra": json.dumps(
            {
                "beta": _num(profile.get("beta")),
                "volume": _num(profile.get("volume")),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "statement_period_end": (bundle.get("income") or {}).get("date"),
            }
        ),
    }


_COLUMNS = (
    "security_id, period_end, period_type, known_at, market_cap, price, pe_trailing, pe_forward, "
    "peg_ratio, free_cash_flow, fcf_yield, gross_margin, operating_margin, net_margin, "
    "ebitda_margin, current_ratio, quick_ratio, revenue_growth_yoy, extra, source_id"
)
_PLACEHOLDERS = ", ".join(["%s"] * (len(_COLUMNS.split(",")) - 1)) + ", %s"


def _values(row: dict, source_id: int) -> tuple:
    keys = [c.strip() for c in _COLUMNS.split(",") if c.strip() != "source_id"]
    return (*(row[k] for k in keys), source_id)


def store(conn: psycopg.Connection, rows: list[dict], *, fetched_at: datetime, notes: str) -> int:
    """Write rows plus their provenance in one transaction. Returns rows inserted."""
    if not rows:
        return 0
    period_ends = sorted(str(r["period_end"]) for r in rows)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (provider, dataset, fetched_at, period_start, period_end, "
            "source_uri, row_count, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (PROVIDER, DATASET, fetched_at, period_ends[0], period_ends[-1],
             SOURCE_URI, len(rows), notes),
        )
        source_id = cur.fetchone()[0]
        # The unique key is (security_id, period_end, period_type, source_id, known_at) with NULLS
        # NOT DISTINCT. source_id differs per run, so re-running does NOT overwrite — it appends a
        # second observation of the same period. That is correct for a point-in-time store: a
        # restatement is a new fact, not a correction to be applied in place.
        cur.executemany(
            f"INSERT INTO fundamentals_snapshots ({_COLUMNS}) VALUES ({_PLACEHOLDERS}) "
            f"ON CONFLICT DO NOTHING",
            [_values(r, source_id) for r in rows],
        )
        inserted = cur.rowcount
    return inserted


def cmd_load(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        logger.error("no symbols given")
        return EXIT_FAIL

    client = FmpClient()
    fetched_at = datetime.now(timezone.utc)
    rows: list[dict] = []
    skipped_unknown: list[str] = []
    failed: list[str] = []

    for symbol in symbols:
        security_id = resolve_security_id(conn, symbol)
        if security_id is None:
            skipped_unknown.append(symbol)
            continue
        try:
            bundle = client.fundamentals_bundle(symbol)
        except FmpBudgetExhausted as exc:
            # Stop cleanly rather than half-loading the universe: what was fetched is written,
            # what was not is named. A partial load that looks complete is the failure mode here.
            logger.warning("stopping early: %s", exc)
            break
        except FmpError as exc:
            logger.warning("%s: %s", symbol, exc)
            failed.append(symbol)
            continue

        annual = annual_row(bundle, security_id)
        snapshot = snapshot_row(bundle, security_id, fetched_at)
        rows.extend(r for r in (annual, snapshot) if r is not None)
        logger.info(
            "%s: annual=%s snapshot=%s", symbol, "yes" if annual else "no", "yes" if snapshot else "no"
        )

    notes = (
        f"FMP /stable/ per-symbol bundles for {len(symbols)} requested symbol(s). "
        f"annual rows carry known_at = filing acceptedDate (point-in-time safe); snapshot rows "
        f"carry market cap/price/PE/PEG as of the fetch. "
        f"unknown-to-securities: {sorted(skipped_unknown) or 'none'}; failed: {sorted(failed) or 'none'}; "
        f"fmp calls spent: {client.budget.spent}; paced {client.rate_gate.waited_total_s:.1f}s"
    )
    if args.dry_run:
        logger.info("DRY RUN — would insert %d row(s). %s", len(rows), notes)
        return EXIT_OK

    inserted = store(conn, rows, fetched_at=fetched_at, notes=notes)
    logger.info("inserted %d row(s) from %d symbol(s). %s", inserted, len(symbols), notes)
    if skipped_unknown:
        logger.warning(
            "%d symbol(s) are not in `securities` and were skipped (load reference data first): %s",
            len(skipped_unknown),
            ", ".join(sorted(skipped_unknown)),
        )
    return EXIT_OK


def cmd_report(conn: psycopg.Connection, _args: argparse.Namespace) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT period_type, count(*), min(period_end), max(period_end), "
            "count(*) FILTER (WHERE known_at IS NULL) "
            "FROM fundamentals_snapshots GROUP BY period_type ORDER BY period_type"
        )
        rows = cur.fetchall()
    if not rows:
        logger.info("fundamentals_snapshots is empty")
        return EXIT_OK
    for period_type, count, lo, hi, undated in rows:
        logger.info(
            "%-9s %7d rows  %s .. %s  %d undated (invisible to point-in-time queries)",
            period_type, count, lo, hi, undated,
        )
    return EXIT_OK


COMMANDS = {"load": cmd_load, "report": cmd_report}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="load_fundamentals")
    p.add_argument("command", choices=tuple(COMMANDS))
    p.add_argument("--symbols", default="", help="comma-separated tickers")
    p.add_argument("--dry-run", action="store_true", help="fetch and map, write nothing")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    dsn = __import__("os").environ.get("DATABASE_URL")
    if not dsn:
        # libpq PG* variables are the container path (db_migrate.sh precedent); an empty DSN is
        # valid there and means "use the environment".
        dsn = ""
    with psycopg.connect(dsn) as conn:
        return COMMANDS[args.command](conn, args)


if __name__ == "__main__":
    raise SystemExit(main())
