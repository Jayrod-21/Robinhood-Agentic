"""GET /api/account — the real account snapshot overlaid with live FMP marks.

Read-only by design: this endpoint computes current value and unrealized P&L from the account's
cost basis and live prices, but exposes no path to place or modify orders.

Holdings come from ``services/broker.py``: a live Alpaca read when credentials are configured,
otherwise the volume-mounted fallback snapshot file, kept current by bin/alpaca_snapshot.py. The payload's
``source`` field says which — do not infer it from this docstring, and do not assume the file.
Prices refresh independently from FMP.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.services.broker import get_snapshot
from app.services.marks import get_marks, resolve_ttl_seconds
from app.services.snapshot import SnapshotError
from app.services.valuation import account_totals, value_position, weight_pct

router = APIRouter(prefix="/api", tags=["account"])


class PositionView(BaseModel):
    symbol: str
    quantity: float
    average_buy_price: float
    current_price: float | None
    cost_basis: float
    market_value: float | None
    unrealized_pl: float | None
    unrealized_pl_pct: float | None
    # Two weight bases, deliberately both exposed (issue #21): the charter's ~25%/name cap
    # (docs/AGENTIC_ROBINHOOD_v1.md §5) is stated against ACCOUNT value, so only
    # weight_account_pct is comparable to that limit. weight_pct (equity-only) is kept for
    # allocation context — with a large cash balance the two differ materially, and showing
    # an equity-basis number next to an account-basis cap would fake a breach.
    weight_pct: float | None  # share of live equity value (priced positions only; EXCLUDES cash)
    weight_account_pct: float | None  # share of live account value (equity + cash) — cap basis
    priced: bool


class AccountView(BaseModel):
    account_masked: str
    nickname: str | None
    generated_at: str
    source: str
    stale_prices: bool  # true if any held symbol could not be priced live
    cash: float
    buying_power: float
    # There is no separate "snapshot" total any more. Under the Robinhood file these named the
    # numbers the export claimed, kept apart from the FMP-priced live_* figures so a stale file
    # could not masquerade as current. The broker is now read live on every request, so both halves
    # came from the same call and differed only by which vendor's mark was used — two fields that
    # no page read and that invited the reader to look for a distinction that no longer exists.
    live_equity_value: float
    live_total_value: float
    total_cost_basis: float
    total_unrealized_pl: float
    total_unrealized_pl_pct: float | None
    positions: list[PositionView]


def _round(value: float | None, dp: int = 2) -> float | None:
    """Round, preserving None. An unknown weight must not become 0.0 on the way to the page."""
    return None if value is None else round(value, dp)


def _build_view() -> AccountView:
    settings = get_settings()
    snapshot = get_snapshot(settings.snapshot_path)
    marks = get_marks(snapshot.symbols, resolve_ttl_seconds(settings.marks_ttl_seconds))

    # First pass: cost basis, market value, P&L per position.
    rows: list[PositionView] = []
    live_equity = 0.0
    total_cost = 0.0
    total_pl = 0.0
    any_unpriced = False

    values = [
        value_position(pos.quantity, pos.average_buy_price, marks.get(pos.symbol))
        for pos in snapshot.positions
    ]
    cash = snapshot.account.cash
    totals = account_totals(values, cash)

    rows = [
        PositionView(
            symbol=pos.symbol,
            quantity=pos.quantity,
            average_buy_price=pos.average_buy_price,
            current_price=round(marks[pos.symbol], 4) if v.priced else None,
            cost_basis=round(v.cost_basis, 2),
            market_value=round(v.market_value, 2) if v.market_value is not None else None,
            unrealized_pl=round(v.unrealized_pl, 2) if v.unrealized_pl is not None else None,
            unrealized_pl_pct=round(v.unrealized_pl_pct, 2) if v.unrealized_pl_pct is not None else None,
            # Two bases, both stated. Equity basis (excludes cash) describes the allocation mix;
            # account basis (equity + cash) is what the charter's ~25%/name cap is written against.
            weight_pct=_round(weight_pct(v.market_value, totals.equity_value)),
            weight_account_pct=_round(weight_pct(v.market_value, totals.total_value)),
            priced=v.priced,
        )
        for pos, v in zip(snapshot.positions, values, strict=True)
    ]

    live_equity = totals.equity_value
    total_cost = totals.total_cost_basis
    total_pl = totals.unrealized_pl
    any_unpriced = totals.any_unpriced
    live_total = totals.total_value

    # Over PRICED cost, not total cost. The old form added an unpriced position's cost to the
    # denominator while its P&L stayed out of the numerator, which treats it as exactly 0% return
    # and drags the headline toward zero on any pricing gap — a fabricated number presented as a
    # measured one. `stale_prices` already tells the page the book is partly unpriced.
    total_pl_pct = totals.unrealized_pl_pct

    return AccountView(
        account_masked=snapshot.account.number_masked,
        nickname=snapshot.account.nickname,
        generated_at=snapshot.generated_at,
        source=snapshot.source,
        stale_prices=any_unpriced,
        cash=round(cash, 2),
        buying_power=round(snapshot.account.buying_power, 2),
        live_equity_value=round(live_equity, 2),
        live_total_value=round(live_total, 2),
        total_cost_basis=round(total_cost, 2),
        total_unrealized_pl=round(total_pl, 2),
        total_unrealized_pl_pct=round(total_pl_pct, 2) if total_pl_pct is not None else None,
        positions=rows,
    )


@router.get("/account", response_model=AccountView)
async def get_account() -> AccountView:
    try:
        # FMP I/O is blocking; keep it off the event loop.
        return await asyncio.to_thread(_build_view)
    except SnapshotError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
