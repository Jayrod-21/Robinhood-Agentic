"""GET /api/account — the real account snapshot overlaid with live yfinance marks.

Read-only by design: this endpoint computes current value and unrealized P&L from the snapshot's
cost basis and live prices, but exposes no path to place or modify orders. Holdings come from the
volume-mounted snapshot (refreshed via the bridge); prices refresh autonomously here.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.services.marks import get_marks
from app.services.snapshot import SnapshotError, load_snapshot

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
    weight_pct: float | None  # share of equity value
    priced: bool


class AccountView(BaseModel):
    account_masked: str
    nickname: str | None
    generated_at: str
    source: str
    stale_prices: bool  # true if any held symbol could not be priced live
    cash: float
    buying_power: float
    snapshot_total_value: float
    snapshot_equity_value: float
    live_equity_value: float
    live_total_value: float
    total_cost_basis: float
    total_unrealized_pl: float
    total_unrealized_pl_pct: float | None
    positions: list[PositionView]


def _build_view() -> AccountView:
    settings = get_settings()
    snapshot = load_snapshot(settings.snapshot_path)
    marks = get_marks(snapshot.symbols, settings.marks_ttl_seconds)

    # First pass: cost basis, market value, P&L per position.
    rows: list[PositionView] = []
    live_equity = 0.0
    total_cost = 0.0
    total_pl = 0.0
    any_unpriced = False

    for pos in snapshot.positions:
        price = marks.get(pos.symbol)
        cost_basis = pos.quantity * pos.average_buy_price
        total_cost += cost_basis

        if price is None:
            any_unpriced = True
            rows.append(
                PositionView(
                    symbol=pos.symbol,
                    quantity=pos.quantity,
                    average_buy_price=pos.average_buy_price,
                    current_price=None,
                    cost_basis=round(cost_basis, 2),
                    market_value=None,
                    unrealized_pl=None,
                    unrealized_pl_pct=None,
                    weight_pct=None,
                    priced=False,
                )
            )
            continue

        market_value = pos.quantity * price
        pl = market_value - cost_basis
        pl_pct = (pl / cost_basis * 100.0) if cost_basis > 0 else None
        live_equity += market_value
        total_pl += pl
        rows.append(
            PositionView(
                symbol=pos.symbol,
                quantity=pos.quantity,
                average_buy_price=pos.average_buy_price,
                current_price=round(price, 4),
                cost_basis=round(cost_basis, 2),
                market_value=round(market_value, 2),
                unrealized_pl=round(pl, 2),
                unrealized_pl_pct=round(pl_pct, 2) if pl_pct is not None else None,
                weight_pct=None,  # filled in second pass once live_equity is known
                priced=True,
            )
        )

    # Second pass: position weights as a share of live equity.
    for row in rows:
        if row.priced and row.market_value is not None and live_equity > 0:
            row.weight_pct = round(row.market_value / live_equity * 100.0, 2)

    total_pl_pct = (total_pl / total_cost * 100.0) if total_cost > 0 else None
    cash = snapshot.account.cash

    return AccountView(
        account_masked=snapshot.account.number_masked,
        nickname=snapshot.account.nickname,
        generated_at=snapshot.generated_at,
        source=snapshot.source,
        stale_prices=any_unpriced,
        cash=round(cash, 2),
        buying_power=round(snapshot.account.buying_power, 2),
        snapshot_total_value=round(snapshot.account.total_value, 2),
        snapshot_equity_value=round(snapshot.account.equity_value, 2),
        live_equity_value=round(live_equity, 2),
        live_total_value=round(live_equity + cash, 2),
        total_cost_basis=round(total_cost, 2),
        total_unrealized_pl=round(total_pl, 2),
        total_unrealized_pl_pct=round(total_pl_pct, 2) if total_pl_pct is not None else None,
        positions=rows,
    )


@router.get("/account", response_model=AccountView)
async def get_account() -> AccountView:
    try:
        # yfinance I/O is blocking; keep it off the event loop.
        return await asyncio.to_thread(_build_view)
    except SnapshotError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
