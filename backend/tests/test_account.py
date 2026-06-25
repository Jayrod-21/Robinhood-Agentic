"""Account overlay math: cost basis, P&L, weights, and unpriced soft-fail."""

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


def test_unpriced_position_is_soft(monkeypatch):
    monkeypatch.setattr(account_mod, "load_snapshot", lambda _p: _snapshot())
    monkeypatch.setattr(account_mod, "get_marks", lambda syms, ttl: {"AAA": 110.0, "BBB": None})

    view = account_mod._build_view()
    bbb = next(p for p in view.positions if p.symbol == "BBB")
    assert bbb.priced is False
    assert bbb.market_value is None and bbb.unrealized_pl is None
    assert view.stale_prices is True
    # equity counts only the priced AAA.
    assert view.live_equity_value == 110.0
