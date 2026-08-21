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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from src import piotroski as piotroski_mod
from src.data import (
    eps_growth_yoy,
    known_at_from_statement,
    piotroski_inputs,
    wide_fundamentals_from_fmp,
)
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


def _filing_is_plausible(period_end: str, known_at: str) -> bool:
    """A statement cannot be filed before the period it reports on has ended.

    Enforced here as well as by ck_fundamentals_known_at so a single bad vendor record costs one
    period rather than aborting the whole load at the INSERT.
    """
    try:
        ends = date.fromisoformat(str(period_end)[:10])
        filed = date.fromisoformat(str(known_at)[:10])
    except ValueError:
        return False
    return filed >= ends


def update_security_profile(conn: psycopg.Connection, security_id: int, bundle: dict) -> bool:
    """Fill in a security's name, sector and industry from the profile we already fetched.

    `securities` is reference data loaded elsewhere, and resolve_security_id deliberately refuses to
    CREATE rows here. Enriching an existing row is a different thing: the profile call has already
    been made and paid for, and leaving 19,745 securities with a NULL name meant every page rendered
    a company as its ticker and an em dash.

    COALESCE keeps whatever a dedicated reference loader may have set — this fills gaps, it does not
    overwrite a better source.
    """
    profile = bundle.get("profile") or {}
    name = (profile.get("companyName") or "").strip() or None
    sector = (profile.get("sector") or "").strip() or None
    industry = (profile.get("industry") or "").strip() or None
    exchange = (profile.get("exchange") or "").strip() or None
    if not any((name, sector, industry, exchange)):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE securities SET name = COALESCE(name, %s), sector = COALESCE(sector, %s),"
            " industry = COALESCE(industry, %s), exchange = COALESCE(exchange, %s), updated_at = now()"
            " WHERE id = %s",
            (name, sector, industry, exchange, security_id),
        )
    return True


def annual_row(bundle: dict, security_id: int, index: int = 0) -> dict | None:
    """Statement-derived figures for the period the statements cover. Real history.

    Returns None when there is no statement to date — a row whose known_at cannot be established
    would be invisible to point-in-time queries anyway (the index excludes NULLs), and writing it
    would inflate the row count with something nothing can read.
    """
    # index selects WHICH filed period this row describes. 0 is the newest; higher indices walk
    # back through the years, which is what turns a single snapshot into a history someone can read
    # a trend from. Falls back to the bundle's top-level (newest) shape when a series is absent.
    per = bundle.get("periods") or {}

    def _at(key: str, fallback_key: str):
        series = per.get(key) or []
        if index < len(series):
            return series[index] or {}
        return (bundle.get(fallback_key) or {}) if index == 0 else {}

    income = _at("income", "income")
    ratios = _at("ratios", "ratios")
    cash_flow = _at("cash_flow", "cash_flow")
    growth = _at("growth", "growth")

    period_end = income.get("date") or ratios.get("date")
    known_at = known_at_from_statement(income) or known_at_from_statement(cash_flow)
    if not period_end or not known_at:
        return None
    if not _filing_is_plausible(period_end, known_at):
        # The vendor sometimes stamps a statement as filed BEFORE the period it reports on closed
        # (UNH FY2023 arrives as accepted 2023-12-29 for a period ending 2023-12-31). That date
        # cannot be true, and the honest move is to drop the observation rather than clamp it:
        # clamping invents a knowability claim, and known_at is the one column a backtest trusts
        # to decide what was knowable when. Missing a year is recoverable; a wrong known_at
        # silently leaks the future into every point-in-time query built on it.
        logger.warning(
            "%s %s: filing stamp %s precedes the period end — implausible, so this period is "
            "dropped rather than dated with a guess",
            income.get("symbol") or f"security {security_id}", period_end, known_at,
        )
        return None

    wide, derived = wide_fundamentals_from_fmp(bundle)
    if index > 0:
        # profile-sourced values describe TODAY. Attaching today's beta or 52-week range to a
        # period that closed years ago would be a fabricated historical fact — the single most
        # tempting mistake in a history table.
        for market_only in ("beta", "week_52_high", "week_52_low", "avg_volume_30d",
                            "analyst_target_price", "analyst_recommendation",
                            "price_to_tangible_book"):
            wide[market_only] = None
        derived = {k: v for k, v in derived.items()
                   if k not in ("week_52_high", "week_52_low", "price_to_tangible_book")}
    periods = bundle.get("periods") or {}
    inc, cf, bal = periods.get("income") or [], periods.get("cash_flow") or [], periods.get("balance") or []

    # Piotroski needs TWO consecutive annual periods. With only one on record the score is not
    # computed at all rather than computed against nothing — a score built from a single year would
    # be seven unknown signals wearing a number.
    # Scored against the period immediately prior to THIS one, so an older row carries the score
    # that was true then rather than today's. The oldest period on record has no predecessor and is
    # therefore unscored — which is honest: a comparison needs two years.
    pio = None
    if len(inc) > index + 1 and len(cf) > index + 1 and len(bal) > index + 1:
        pio = piotroski_mod.score(
            piotroski_inputs(inc[index], cf[index], bal[index]),
            piotroski_inputs(inc[index + 1], cf[index + 1], bal[index + 1]),
        )

    row = {
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
        **wide,
        "eps_growth_yoy": eps_growth_yoy(inc[index:]),
        "derived_fields": json.dumps(derived) if derived else None,
        "piotroski_f_score": pio["score"] if pio and pio["complete"] else None,
        # The variant travels WITH the score, always: migration 016 enforces it, because a score
        # whose definition is unknown cannot be compared to anything, including Bloomberg's.
        "piotroski_variant": pio["variant"] if pio and pio["complete"] else None,
        "piotroski_signals": json.dumps(pio) if pio else None,
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
    return row


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
        # NOT AVAILABLE on this plan, and therefore None rather than a stand-in.
        #
        # This read forwardPriceToEarningsGrowthRatio — a forward PEG — into a column named
        # pe_forward. FMP offers priceToEarningsRatio (trailing), priceToEarningsGrowthRatio and
        # forwardPriceToEarningsGrowthRatio; there is no forward P/E among them. So every row
        # reported NVDA's forward P/E as 0.57, byte-identical to its PEG, which is not a plausible
        # multiple for anything and was being shown on the dashboard as one.
        #
        # Found by a bear researcher in a live debate, which called it "almost certainly a data
        # error" while arguing against the bull case. Worth recording how it surfaced: no test
        # caught it, because a test would have asserted the column was populated, and it was.
        "pe_forward": None,
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
    "ebitda_margin, current_ratio, quick_ratio, revenue_growth_yoy, "
    # 016: the rest of the Bloomberg pull.
    "dividend_yield, ev_to_ebitda, price_to_book, price_to_sales, price_to_tangible_book, beta, "
    "week_52_high, week_52_low, avg_volume_30d, revenue_ttm, ebitda_ttm, capital_expenditure, "
    "net_debt, shares_outstanding, tangible_book_value_per_share, eps_growth_yoy, rd_to_revenue, "
    "equity_to_assets, roe, roc, debt_to_equity, ebitda_interest, cash_conversion_cycle, "
    "eps_current, eps_next_year_est, short_interest, analyst_target_price, "
    "analyst_recommendation, derived_fields, piotroski_f_score, piotroski_variant, "
    "piotroski_signals, extra, source_id"
)
_PLACEHOLDERS = ", ".join(["%s"] * (len(_COLUMNS.split(",")) - 1)) + ", %s"


def _values(row: dict, source_id: int) -> tuple:
    """Row -> tuple in _COLUMNS order, with absent keys written as NULL.

    `.get`, not `[]`: the two row kinds carry different columns ON PURPOSE. An annual row has
    margins and Piotroski and no market cap; a snapshot row has market cap and price and no
    statement figures. Demanding every column from both would force each to carry placeholder
    values for the other's fields — which is exactly the merging the two-row design exists to
    prevent.
    """
    keys = [c.strip() for c in _COLUMNS.split(",") if c.strip() != "source_id"]
    return (*(row.get(k) for k in keys), source_id)


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
        # UPSERT on the observation key (017): re-running is the SAME observation with a possibly
        # better mapping, so it updates in place. A genuine restatement carries a new known_at and
        # therefore lands as a new row, which is the behaviour the append-only design wanted and
        # the old source_id-keyed constraint failed to deliver.
        updatable = [
            c.strip() for c in _COLUMNS.split(",")
            if c.strip() not in ("security_id", "period_end", "period_type", "known_at")
        ]
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        cur.executemany(
            f"INSERT INTO fundamentals_snapshots ({_COLUMNS}) VALUES ({_PLACEHOLDERS}) "
            f"ON CONFLICT (security_id, period_end, period_type, known_at) "
            f"DO UPDATE SET {set_clause}, updated_at = now()",
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
    enriched: list[str] = []

    for symbol in symbols:
        security_id = resolve_security_id(conn, symbol)
        if security_id is None:
            skipped_unknown.append(symbol)
            continue
        try:
            bundle = client.fundamentals_bundle(symbol, periods=max(2, args.periods))
        except FmpBudgetExhausted as exc:
            # Stop cleanly rather than half-loading the universe: what was fetched is written,
            # what was not is named. A partial load that looks complete is the failure mode here.
            logger.warning("stopping early: %s", exc)
            break
        except FmpError as exc:
            logger.warning("%s: %s", symbol, exc)
            failed.append(symbol)
            continue

        if update_security_profile(conn, security_id, bundle):
            enriched.append(symbol)

        periods_available = len((bundle.get("periods") or {}).get("income") or [])
        annuals = [
            r for i in range(max(1, periods_available))
            if (r := annual_row(bundle, security_id, i)) is not None
        ]
        snapshot = snapshot_row(bundle, security_id, fetched_at)
        rows.extend(annuals)
        if snapshot is not None:
            rows.append(snapshot)
        logger.info("%s: %d annual period(s), snapshot=%s",
                    symbol, len(annuals), "yes" if snapshot else "no")

    notes = (
        f"FMP /stable/ per-symbol bundles for {len(symbols)} requested symbol(s). "
        f"annual rows carry known_at = filing acceptedDate (point-in-time safe); snapshot rows "
        f"carry market cap/price/PE/PEG as of the fetch. "
        f"unknown-to-securities: {sorted(skipped_unknown) or 'none'}; failed: {sorted(failed) or 'none'}; "
        f"securities enriched with name/sector: {len(enriched)}; "
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
    p.add_argument("--periods", type=int, default=2,
                   help="annual periods per symbol (floored at 2 — Piotroski needs a prior year)")
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
