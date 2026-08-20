"""Reconciliation and market context.

The reconciliation endpoint answers issue #22 — the comparison nobody could make, because the plan
lives in markdown and the holdings live at a broker. The tests below are mostly about it refusing
to report agreement it has not measured.
"""

from __future__ import annotations

import pytest
from app.routers import market_context as mc
from app.routers import reconciliation as rec
from app.services.snapshot import AccountSnapshot
from fastapi import HTTPException


def _snapshot(positions=(), cash=500.0, total=1000.0):
    return AccountSnapshot.model_validate(
        {
            "schema_version": 1,
            "source": "alpaca-paper",
            "generated_at": "2026-08-17T12:00:00Z",
            "account": {
                "number_masked": "••••I1PN", "total_value": total,
                "equity_value": total - cash, "cash": cash, "buying_power": cash,
            },
            "positions": [
                {"symbol": s, "quantity": q, "average_buy_price": c} for s, q, c in positions
            ],
        }
    )


# ── reconciliation ────────────────────────────────────────────────────────────────────────────


def test_an_empty_account_reports_every_slate_name_missing(monkeypatch):
    """The live case today: a fresh paper account against a slate written for another broker.
    Reporting 'in sync' here would be the single most misleading answer this endpoint could give."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot(cash=1000.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {})
    body = rec.reconciliation()
    assert body["meta"]["in_sync"] is False
    assert body["summary"]["missing"] == 8
    assert body["summary"]["matched"] == 0
    assert all(r["status"] == "missing" for r in body["positions"])


def test_a_holding_at_target_matches(monkeypatch):
    monkeypatch.setattr(
        rec, "get_snapshot", lambda _p: _snapshot([("TSM", 22.0, 10.0)], cash=780.0)
    )
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"TSM": 10.0})
    body = rec.reconciliation()
    tsm = next(r for r in body["positions"] if r["symbol"] == "TSM")
    assert tsm["live_weight_pct"] == pytest.approx(22.0)
    assert tsm["status"] == "match"
    # Against the threshold the endpoint REPORTS, not a module constant. The thresholds are
    # operator-tunable now (migration 019), so meta is the contract — and a test reading a constant
    # the page never sees would keep passing while the two disagreed.
    assert abs(tsm["drift_pct"]) <= body["meta"]["drift_tolerance_pct"]


def test_a_holding_past_tolerance_is_drifted(monkeypatch):
    monkeypatch.setattr(
        rec, "get_snapshot", lambda _p: _snapshot([("TSM", 30.0, 10.0)], cash=700.0)
    )
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"TSM": 10.0})
    body = rec.reconciliation()
    tsm = next(r for r in body["positions"] if r["symbol"] == "TSM")
    assert tsm["status"] == "drifted"
    assert tsm["drift_pct"] == pytest.approx(8.0)
    # Relative drift is reported too: 8 points on a 22% target is 36%, which reads very differently
    # from 8 points on a 2% target.
    assert tsm["drift_rel_pct"] == pytest.approx(36.4, abs=0.2)


def test_a_held_name_not_in_the_slate_is_unexpected_not_an_error(monkeypatch):
    """Some of the most useful rows this endpoint produces are positions nobody wrote down."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot([("MU", 10.0, 10.0)], cash=900.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"MU": 10.0})
    body = rec.reconciliation()
    mu = next(r for r in body["positions"] if r["symbol"] == "MU")
    assert mu["status"] == "unexpected"
    assert mu["target_weight_pct"] is None
    assert mu["in_universe"] is False
    assert body["summary"]["unexpected"] == 1


def test_an_unpriced_holding_is_never_reported_as_matching(monkeypatch):
    """Calling an unmeasurable position 'match' asserts agreement nobody checked."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot([("TSM", 22.0, 10.0)], cash=780.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"TSM": None})
    body = rec.reconciliation()
    tsm = next(r for r in body["positions"] if r["symbol"] == "TSM")
    assert tsm["priced"] is False
    assert tsm["live_weight_pct"] is None
    assert tsm["status"] == "drifted", "unknown must not resolve to match"


def test_checks_report_passes_as_well_as_breaches(monkeypatch):
    """A rule that only appears when broken leaves 'was this even checked?' unanswerable."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot(cash=1000.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {})
    body = rec.reconciliation()
    keys = {c["rule"] for c in body["checks"]}
    assert len(keys) == 4
    assert any(c["status"] == "pass" for c in body["checks"])
    assert {c["status"] for c in body["checks"]} <= {"pass", "breach"}


def test_the_cash_band_breach_is_reported(monkeypatch):
    """100% cash is outside the 10-20% band, and that is a finding rather than a comfortable state."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot(cash=1000.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {})
    body = rec.reconciliation()
    cash_check = next(c for c in body["checks"] if "Cash" in c["rule"])
    assert cash_check["status"] == "breach"
    assert cash_check["severity"] == "warn"


def test_a_breached_stop_is_an_alert(monkeypatch):
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot([("TSM", 10.0, 10.0)], cash=900.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"TSM": 7.0})  # -30%
    body = rec.reconciliation()
    stop_check = next(c for c in body["checks"] if "Hard stop" in c["rule"])
    assert stop_check["status"] == "breach"
    assert stop_check["severity"] == "alert"
    assert "TSM" in stop_check["detail"]


def test_an_unreadable_slate_is_503_not_an_empty_reconciliation(monkeypatch):
    """'The slate did not parse' and 'the broker holds nothing documented' are opposite conclusions.
    Rendering the first as the second reports a parser failure as a portfolio finding."""
    monkeypatch.setattr(rec, "load_slate", lambda _p: {})
    with pytest.raises(HTTPException) as exc:
        rec.reconciliation()
    assert exc.value.status_code == 503


def test_the_documented_book_size_is_surfaced(monkeypatch):
    """A gap between the slate's assumed book and the live account means deposits nobody recorded —
    or a broker migration. Either way it is the operator's to explain, not ours to hide.

    The slate was restated onto the Alpaca account on 2026-08-18, so the documented book is now
    $100,000 rather than the $100 Robinhood bootstrap. This reads the REAL docs/SLATE.md, so it goes
    red if that header is edited without anyone thinking about what reconciliation will report."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot(cash=1000.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {})
    meta = rec.reconciliation()["meta"]
    assert meta["documented_book_value"] == 100_000.0
    assert meta["account_value"] == 1000.0
    assert meta["slate_dated"] == "2026-06-03"


# ── market context ────────────────────────────────────────────────────────────────────────────


def test_no_brief_is_distinguishable_from_a_quiet_day(monkeypatch):
    """An empty list dressed as a quiet news day is unactionable; 'nothing published' is a reason to
    go looking."""
    monkeypatch.setattr(mc, "_load_brief", lambda: None)
    monkeypatch.setattr(mc, "_catalysts", lambda *a, **k: [])
    monkeypatch.setattr(mc, "get_snapshot", lambda _p: _snapshot())
    body = mc.market_context()
    assert body["meta"]["brief_present"] is False
    assert body["meta"]["brief_generated_at"] is None
    assert body["meta"]["brief_stale"] is True
    assert body["headlines"] == []


def test_headline_tickers_are_filtered_to_names_the_book_cares_about(monkeypatch):
    """The relevance chips drive attention. A headline tagged with a name nobody holds or documents
    would put a chip on screen that means nothing here."""
    monkeypatch.setattr(mc, "get_snapshot", lambda _p: _snapshot([("TSM", 1.0, 10.0)]))
    monkeypatch.setattr(mc, "_catalysts", lambda *a, **k: [])
    monkeypatch.setattr(
        mc, "_load_brief",
        lambda: {
            "generated_at": "2026-08-17T06:00:00Z",
            "macro_read": "Risk-on into the print.",
            "headlines": [
                {"id": "h1", "title": "TSMC lifts outlook", "tickers": ["TSM", "AAPL", "ZZZZ"]}
            ],
        },
    )
    body = mc.market_context()
    assert body["headlines"][0]["tickers"] == ["TSM"], "only slate or held names earn a chip"
    assert body["meta"]["macro_read"] == "Risk-on into the print."


def test_a_malformed_brief_is_absent_not_fatal(monkeypatch, tmp_path, caplog):
    """Catalysts are still worth showing. But it is logged loudly: serving no headlines when a brief
    EXISTS is different from none being published."""
    bad = tmp_path / "latest.json"
    bad.write_text("{not json")
    monkeypatch.setattr(mc, "_brief_path", lambda: bad)
    monkeypatch.setattr(mc, "_catalysts", lambda *a, **k: [])
    monkeypatch.setattr(mc, "get_snapshot", lambda _p: _snapshot())
    with caplog.at_level("ERROR"):
        body = mc.market_context()
    assert body["headlines"] == []
    assert any("unreadable" in r.getMessage() for r in caplog.records)


def test_trading_days_skip_weekends():
    """A rental window computed on calendar days opens too early: three days over a weekend is one
    trading day."""
    from datetime import timedelta

    from app.routers.market_context import _trading_days_until

    today = mc.datetime.now(mc.timezone.utc).date()
    assert _trading_days_until(today) == 0
    assert _trading_days_until(today - timedelta(days=3)) == 0, "a past date is not in the future"
    ahead = _trading_days_until(today + timedelta(days=14))
    assert 9 <= ahead <= 11, f"14 calendar days should be ~10 trading days, got {ahead}"


def test_the_account_being_unreadable_still_returns_catalysts(monkeypatch):
    """Slate-only relevance is a degraded answer, not a blank page."""
    from app.services.snapshot import SnapshotError

    def boom(_p):
        raise SnapshotError("broker down")

    monkeypatch.setattr(mc, "get_snapshot", boom)
    monkeypatch.setattr(mc, "_load_brief", lambda: None)
    monkeypatch.setattr(mc, "_catalysts", lambda symbols, slate, held: [
        {"symbol": "NVDA", "held": "NVDA" in held, "in_slate": "NVDA" in slate}
    ])
    body = mc.market_context()
    assert body["catalysts"][0]["in_slate"] is True
    assert body["catalysts"][0]["held"] is False, "unconfirmed holdings report as not held"


# ── tunable thresholds ────────────────────────────────────────────────────────────────────────


def test_thresholds_come_from_settings_and_say_where_they_came_from(monkeypatch):
    """Reconciliation reads its guardrails from app_settings now. It must also report WHICH source
    it used: a breach judged against a compiled default, while the operator believes they set
    something else, is a guardrail misreporting what it enforced."""
    from app.services import settings_store

    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot([("TSM", 24.0, 10.0)], cash=760.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"TSM": 10.0})
    monkeypatch.setattr(
        settings_store, "get_all",
        lambda: ({**settings_store.defaults(), "drift_tolerance_pct": 5.0}, "database"),
    )
    body = rec.reconciliation()
    tsm = next(r for r in body["positions"] if r["symbol"] == "TSM")
    assert body["meta"]["drift_tolerance_pct"] == 5.0
    assert body["meta"]["thresholds_source"] == "database"
    # 24% against a 22% target is 2 points of drift: drifted at the 1.5 default, matched at 5.0.
    assert tsm["status"] == "match", "the widened tolerance was not applied"


def test_a_database_outage_leaves_reconciliation_working_on_defaults(monkeypatch):
    """This route reads a broker snapshot and a markdown file — neither needs Postgres. Letting a
    settings lookup take the page down would lose the answer while both its inputs are readable."""
    from app.db import DbUnavailable
    from app.services import settings_store

    settings_store.reset_cache()

    def boom():
        raise DbUnavailable("down", "the database is unavailable")

    monkeypatch.setattr(settings_store, "connection", lambda: boom())
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot(cash=1000.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {})

    body = rec.reconciliation()
    assert body["meta"]["thresholds_source"] == "defaults", "the fallback must be declared, not silent"
    assert body["meta"]["drift_tolerance_pct"] == settings_store.defaults()["drift_tolerance_pct"]
    assert body["positions"], "the page still answers without the database"
    settings_store.reset_cache()


def test_an_unpriced_off_factor_name_makes_the_floor_unknown_not_breached(monkeypatch):
    """A guardrail must not fire on a data outage and point at the portfolio.

    `sum(r["live_weight_pct"] or 0.0)` turned an unknown weight into zero, so a transient pricing
    gap on V or CVX flipped "V+CVX >= 20%" to breach while both were held at full weight. Everywhere
    else this route treats unknown as unknown — an unpriced holding is "drifted", never "match" —
    and this one check resolved unknown to the worst case instead.
    """
    monkeypatch.setattr(
        rec, "get_snapshot",
        lambda _p: _snapshot([("V", 30.0, 10.0), ("CVX", 30.0, 10.0)], cash=400.0),
    )
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"V": None, "CVX": 10.0})

    check = next(c for c in rec.reconciliation()["checks"] if "Off-factor" in c["rule"])
    assert check["status"] == "unknown", "an unmeasurable floor is not a breached one"
    assert "V" in check["detail"], "the detail must name what could not be priced"
    assert "unpriced" in check["detail"]


def test_the_off_factor_floor_still_breaches_when_it_is_genuinely_short(monkeypatch):
    """Guards the test above: if 'unknown' swallowed every case, a real breach would go unreported."""
    monkeypatch.setattr(
        rec, "get_snapshot", lambda _p: _snapshot([("V", 1.0, 10.0), ("CVX", 1.0, 10.0)], cash=980.0)
    )
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {"V": 10.0, "CVX": 10.0})

    check = next(c for c in rec.reconciliation()["checks"] if "Off-factor" in c["rule"])
    assert check["status"] == "breach"


def test_check_statuses_are_a_known_set(monkeypatch):
    """'unknown' joins pass and breach as a legitimate verdict. A check that cannot be evaluated is
    a third state, and collapsing it into either of the other two loses the distinction that
    matters most: whether the rule was actually applied."""
    monkeypatch.setattr(rec, "get_snapshot", lambda _p: _snapshot(cash=1000.0))
    monkeypatch.setattr(rec, "get_marks", lambda syms, ttl: {})
    statuses = {c["status"] for c in rec.reconciliation()["checks"]}
    assert statuses <= {"pass", "breach", "unknown"}
