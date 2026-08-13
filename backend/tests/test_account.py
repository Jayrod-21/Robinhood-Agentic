"""Account overlay math: cost basis, P&L, weights (both bases), and unpriced soft-fail."""

import pytest

from app.routers import account as account_mod
from app.services.snapshot import AccountSnapshot


def _snapshot():
    return AccountSnapshot.model_validate(
        {
            "schema_version": 1,
            "source": "robinhood-mcp",
            "generated_at": "2026-06-16T00:00:00Z",
            "account": {"number_masked": "••••4025", "total_value": 160.0, "equity_value": 110.0, "cash": 50.0, "buying_power": 50.0},
            "positions": [
                {"symbol": "AAA", "quantity": 1.0, "average_buy_price": 100.0},
                {"symbol": "BBB", "quantity": 2.0, "average_buy_price": 10.0},
            ],
        }
    )


def test_pl_and_weights(monkeypatch):
    monkeypatch.setattr(account_mod, "load_snapshot", lambda _p: _snapshot())
    monkeypatch.setattr(account_mod, "get_marks", lambda syms, ttl: {"AAA": 110.0, "BBB": 12.0})

    view = account_mod._build_view()
    # AAA: mv 110, cost 100, pl +10. BBB: mv 24, cost 20, pl +4. equity 134, cash 50.
    assert view.live_equity_value == 134.0
    assert view.live_total_value == 184.0
    assert view.total_cost_basis == 120.0
    assert view.total_unrealized_pl == 14.0
    aaa = next(p for p in view.positions if p.symbol == "AAA")
    assert aaa.unrealized_pl == 10.0
    assert round(aaa.weight_pct, 1) == round(110.0 / 134.0 * 100, 1)
    assert view.stale_prices is False


def test_weight_bases_equity_vs_account(monkeypatch):
    """Regression for issue #21: each weight field must stay on its stated basis.

    The charter's ~25%/name cap (docs/AGENTIC_ROBINHOOD_v1.md section 5) is written against
    ACCOUNT value, so weight_account_pct must divide by equity + cash, while weight_pct divides
    by equity only. The fixture holds cash as a large share of the book (50 of 184, ~27%) so the
    two bases differ materially — if either denominator is swapped the assertions below go red.
    """
    monkeypatch.setattr(account_mod, "load_snapshot", lambda _p: _snapshot())
    monkeypatch.setattr(account_mod, "get_marks", lambda syms, ttl: {"AAA": 110.0, "BBB": 12.0})

    view = account_mod._build_view()
    # Live equity 134 (AAA 110 + BBB 24), cash 50 -> account value 184.
    assert view.live_equity_value == 134.0
    assert view.live_total_value == 184.0

    aaa = next(p for p in view.positions if p.symbol == "AAA")
    bbb = next(p for p in view.positions if p.symbol == "BBB")

    # Equity basis: market_value / live_equity (EXCLUDES cash).
    assert aaa.weight_pct == round(110.0 / 134.0 * 100.0, 2)  # 82.09
    assert bbb.weight_pct == round(24.0 / 134.0 * 100.0, 2)  # 17.91
    # Account basis: market_value / (live_equity + cash) — the cap-comparable number.
    assert aaa.weight_account_pct == round(110.0 / 184.0 * 100.0, 2)  # 59.78
    assert bbb.weight_account_pct == round(24.0 / 184.0 * 100.0, 2)  # 13.04

    # The two bases must actually differ here, or this test proves nothing about the denominators.
    assert abs(aaa.weight_pct - aaa.weight_account_pct) > 10.0
    # Account-basis weights plus the cash share account for the whole book.
    cash_share = 50.0 / 184.0 * 100.0
    assert aaa.weight_account_pct + bbb.weight_account_pct + cash_share == pytest.approx(100.0, abs=0.02)


def test_unpriced_position_is_soft(monkeypatch):
    monkeypatch.setattr(account_mod, "load_snapshot", lambda _p: _snapshot())
    monkeypatch.setattr(account_mod, "get_marks", lambda syms, ttl: {"AAA": 110.0, "BBB": None})

    view = account_mod._build_view()
    bbb = next(p for p in view.positions if p.symbol == "BBB")
    assert bbb.priced is False
    assert bbb.market_value is None and bbb.unrealized_pl is None
    assert bbb.weight_pct is None and bbb.weight_account_pct is None
    assert view.stale_prices is True
    # equity counts only the priced AAA.
    assert view.live_equity_value == 110.0
    # AAA's account-basis weight uses priced equity (110) + cash (50).
    aaa = next(p for p in view.positions if p.symbol == "AAA")
    assert aaa.weight_account_pct == round(110.0 / 160.0 * 100.0, 2)  # 68.75
