"""GET /api/fundamentals — the full fundamental set, with history.

Serves what db/load_fundamentals.py stores: the wide field list from the owner's Bloomberg pull,
Cary's Piotroski with its nine signals, and every annual observation on record.

TWO ROW KINDS, MERGED FOR DISPLAY BUT NEVER IN STORAGE
    An `annual` row carries statement figures dated by the filing's acceptance; a `snapshot` row
    carries market figures as of a fetch. They are stored apart because merging them invents a
    record that looks point-in-time and is not (migration 003 / db/load_fundamentals.py).

    A page, though, wants one line per company. So they are joined HERE, at the edge, with
    `as_of` fields naming when each half was true. The merge is a presentation decision and it is
    reversible; doing it in the database would not be.

HISTORY IS THE ANNUAL SERIES
    One row per filed period, oldest to newest, each with the acceptance date that makes it
    point-in-time safe. That is what makes "has this business been improving" answerable rather
    than a single snapshot repeated.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.db import DbUnavailable, connection
from app.validation import normalize_ticker

logger = logging.getLogger("agentic.api.fundamentals")

router = APIRouter(prefix="/api", tags=["fundamentals"])

# Ordered so the response reads like the owner's sheet rather than like the table's column order.
_ANNUAL_FIELDS = [
    "period_end", "known_at", "revenue_ttm", "ebitda_ttm", "eps_current", "eps_growth_yoy",
    "free_cash_flow", "fcf_yield", "capital_expenditure", "net_debt", "shares_outstanding",
    "gross_margin", "operating_margin", "net_margin", "ebitda_margin",
    "roe", "roc", "current_ratio", "quick_ratio", "debt_to_equity", "equity_to_assets",
    "ebitda_interest", "cash_conversion_cycle", "revenue_growth_yoy", "rd_to_revenue",
    "tangible_book_value_per_share", "piotroski_f_score", "piotroski_variant", "piotroski_signals",
    "derived_fields",
]
_MARKET_FIELDS = [
    "period_end", "known_at", "price", "market_cap", "pe_trailing", "pe_forward", "peg_ratio",
    "price_to_book", "price_to_sales", "price_to_tangible_book", "ev_to_ebitda", "dividend_yield",
    "beta", "week_52_high", "week_52_low", "avg_volume_30d", "analyst_target_price",
    "analyst_recommendation",
]


def _row_to_dict(cols: list[str], row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in zip(cols, row, strict=True):
        if value is None:
            out[name] = None
        elif name in ("piotroski_signals", "derived_fields"):
            out[name] = value  # already jsonb -> dict
        elif name in ("period_end", "known_at"):
            out[name] = str(value)
        elif name in ("piotroski_f_score", "avg_volume_30d"):
            out[name] = int(value)
        elif name in ("piotroski_variant", "analyst_recommendation"):
            out[name] = value
        else:
            out[name] = float(value)
    return out


def _fetch(conn, symbol: str) -> dict[str, Any] | None:
    sec = conn.execute(
        "SELECT id, symbol, name, sector, industry FROM securities WHERE upper(symbol)=upper(%s)",
        (symbol,),
    ).fetchone()
    if sec is None:
        return None
    sec_id, sym, name, sector, industry = sec

    annual_cols = ", ".join(_ANNUAL_FIELDS)
    history = [
        _row_to_dict(_ANNUAL_FIELDS, r)
        for r in conn.execute(
            f"SELECT {annual_cols} FROM fundamentals_snapshots"
            " WHERE security_id=%s AND period_type='annual' ORDER BY period_end DESC",
            (sec_id,),
        ).fetchall()
    ]
    market_cols = ", ".join(_MARKET_FIELDS)
    market_row = conn.execute(
        f"SELECT {market_cols} FROM fundamentals_snapshots"
        " WHERE security_id=%s AND period_type='snapshot' ORDER BY known_at DESC LIMIT 1",
        (sec_id,),
    ).fetchone()

    return {
        "symbol": sym,
        "name": name,
        "sector": sector,
        "industry": industry,
        # Named `market` and `latest_annual` rather than flattened into one object: a single blob
        # would hide that the two halves were true at different moments.
        "market": _row_to_dict(_MARKET_FIELDS, market_row) if market_row else None,
        "latest_annual": history[0] if history else None,
        "history": history,
        "periods_on_record": len(history),
    }


@router.get("/fundamentals")
def fundamentals_list(
    # Annotated rather than `symbols: str = Query("")`: with the latter the DEFAULT VALUE is a Query
    # object, so the parameter only becomes a string when FastAPI resolves it. Any internal caller
    # (a test, a script, another router) gets the sentinel and fails on .split(). The default here
    # is a real string.
    symbols: Annotated[str, Query(description="comma-separated; default: held names")] = "",
) -> dict:
    """One line per company, for the table. Defaults to what the account actually holds."""
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    try:
        with connection() as conn:
            if not wanted:
                wanted = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT s.symbol FROM paper_portfolio_positions p"
                        " JOIN securities s ON s.id=p.security_id"
                        " JOIN paper_portfolios pp ON pp.id=p.portfolio_id"
                        " WHERE pp.kind='real' AND p.exit_date IS NULL ORDER BY 1"
                    ).fetchall()
                ]
            rows = [r for sym in wanted if (r := _fetch(conn, sym)) is not None]
            unknown = [s for s in wanted if s not in {r["symbol"].upper() for r in rows}]
    except DbUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"The database is unavailable: {exc}") from None

    return {
        "meta": {
            "requested": wanted,
            # Named rather than silently dropped: a symbol missing from the table because it is not
            # in `securities` looks identical to one with no fundamentals until someone says so.
            "unknown_symbols": unknown,
            "count": len(rows),
            "piotroski_variant": "cary",
        },
        "rows": rows,
    }


@router.get("/fundamentals/{symbol}")
def fundamentals_one(symbol: str) -> dict:
    ticker = normalize_ticker(symbol)
    if ticker is None:
        raise HTTPException(status_code=400, detail="Not a valid ticker symbol.")
    try:
        with connection() as conn:
            row = _fetch(conn, ticker)
    except DbUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"The database is unavailable: {exc}") from None
    if row is None:
        raise HTTPException(status_code=404, detail=f"{ticker} is not in the securities table.")
    if not row["history"] and not row["market"]:
        # A known security with nothing ingested is a DIFFERENT state from an unknown ticker, and
        # the page should say "not fetched yet" rather than "no such company".
        row["meta_note"] = (
            f"{ticker} is a known security but no fundamentals have been ingested for it yet. "
            f"Run db/load_fundamentals.py load --symbols {ticker}."
        )
    return row
