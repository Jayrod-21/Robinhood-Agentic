"""The position drill-down, and the parsers under it.

Most of these run against the REAL docs/SLATE.md and docs/THESES.md rather than fixtures. That is
deliberate: those files are hand-maintained prose, the parsers are the thing most likely to break
when someone edits them, and a fixture would keep passing while the live files stopped parsing.
A red test here is the intended way to find out that a heading changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.routers import position as pos
from app.services.slate import (
    load_sizing_rules,
    load_slate,
    load_theses,
    slate_health,
)
from app.services.snapshot import AccountSnapshot
from fastapi import HTTPException

# The live docs/SLATE.md is retired (marked NOT IN FORCE) since the account of record moved to the
# Alpaca paper book. These tests exercise the reconciliation ARITHMETIC against the real targets, so
# they pin the slate as governing rather than depending on the owner's current status line. The
# status behaviour itself is tested in tests/test_slate_not_in_force.py.
pytestmark = pytest.mark.usefixtures("slate_in_force")

DOCS = Path(__file__).resolve().parents[2] / "docs"


# ── the parsers, against the live documents ───────────────────────────────────────────────────


def test_the_real_slate_parses():
    """Zero rows means the table format changed and every target on the site silently became
    absent. That must be a red test, not a quiet empty page."""
    slate = load_slate(DOCS / "SLATE.md")
    assert slate, "docs/SLATE.md parsed to zero rows — the table format changed"
    assert "TSM" in slate
    entry = slate["TSM"]
    assert entry.target_weight_pct == 22.0
    assert entry.role and entry.size_rationale, "role and rationale are columns the page renders"


def test_cash_is_not_a_position():
    """CASH is a row in the table and belongs in meta, never in the positions list — rendering it
    as a holding would put a tradable-looking card on a page for something that is not a security."""
    assert "CASH" not in load_slate(DOCS / "SLATE.md")


def test_targets_sum_to_a_sane_book():
    slate = load_slate(DOCS / "SLATE.md")
    total = sum(e.target_weight_pct for e in slate.values())
    assert 85.0 <= total <= 100.0, f"slate targets sum to {total}, which is not a whole book"


def test_every_slate_name_has_a_thesis():
    """The charter's sell-discipline rule in test form: a documented position with no written case
    is the finding this dashboard exists to surface. If this goes red, either a thesis was dropped
    or the heading format changed — both worth knowing immediately."""
    slate = load_slate(DOCS / "SLATE.md")
    theses = load_theses(DOCS / "THESES.md")
    health = slate_health(slate, theses)
    assert health["slate_without_thesis"] == [], (
        f"slate names with no thesis on record: {health['slate_without_thesis']}"
    )


def test_thesis_summary_is_the_case_not_the_heading():
    """The heading is a title carrying markdown ('· **conviction MED**'). Rendering that as the
    thesis would put formatting characters on screen and say nothing about why the position
    exists."""
    theses = load_theses(DOCS / "THESES.md")
    tsm = theses["TSM"]
    assert tsm.core, "no Core thesis line parsed for TSM"
    assert "**" not in tsm.core, "emphasis markers must not reach the page"
    assert len(tsm.core) > 40, "a one-line summary is not a thesis"
    assert tsm.conviction == "HIGH"


def test_sizing_rules_come_from_the_document():
    """Hardcoding these would mean an owner editing SLATE.md and the dashboard disagreeing about
    the stop, with the dashboard winning silently."""
    rules = load_sizing_rules(DOCS / "SLATE.md")
    assert rules.hard_stop_pct == -20.0
    assert rules.trim_multiple == 1.3


def test_an_unparseable_percent_yields_nothing_rather_than_zero(tmp_path):
    """The dangerous failure: a parser that returns a target of 0.0 for TSM renders as 'you are 22
    points overweight' on a page an owner might act on. Missing is safe; wrong is not.

    The row here is otherwise WELL FORMED — bolded ticker, right column count — and differs from a
    real one only in the percent being prose. An earlier version of this test used an unbolded
    ticker, so the row was skipped for failing the ticker pattern and the percent handling was
    never exercised at all: it passed against a parser that coerced bad percents to 0.0.
    """
    broken = tmp_path / "SLATE.md"
    broken.write_text(
        "| Ticker | % | $ | Role | Why |\n|---|---|---|---|---|\n"
        "| **TSM**  | twenty-two | $22 | Compute anchor | because |\n"
    )
    parsed = load_slate(broken)
    assert "TSM" not in parsed, (
        f"an unparseable percent must skip the row, not become a number; got {parsed}"
    )


def test_a_well_formed_row_still_parses(tmp_path):
    """Guards the test above: if the fixture format drifts so that NOTHING matches, the assertion
    that TSM is absent would pass for the wrong reason. This proves the same shape does parse."""
    good = tmp_path / "SLATE.md"
    good.write_text(
        "| Ticker | % | $ | Role | Why |\n|---|---|---|---|---|\n"
        "| **TSM**  | 22 | $22 | Compute anchor | because |\n"
    )
    assert load_slate(good)["TSM"].target_weight_pct == 22.0


def test_a_missing_file_is_empty_not_an_exception(tmp_path):
    assert load_slate(tmp_path / "nope.md") == {}
    assert load_theses(tmp_path / "nope.md") == {}


# ── the endpoint ──────────────────────────────────────────────────────────────────────────────


def _snapshot(positions=()):
    return AccountSnapshot.model_validate(
        {
            "schema_version": 1,
            "source": "alpaca-paper",
            "generated_at": "2026-08-17T12:00:00Z",
            "account": {
                "number_masked": "••••I1PN", "total_value": 1000.0, "equity_value": 500.0,
                "cash": 500.0, "buying_power": 500.0,
            },
            "positions": [
                {"symbol": s, "quantity": q, "average_buy_price": c} for s, q, c in positions
            ],
        }
    )


@pytest.fixture()
def no_holdings(monkeypatch):
    monkeypatch.setattr(pos, "get_snapshot", lambda _p, _acct=None: _snapshot())
    monkeypatch.setattr(pos, "get_marks", lambda syms, ttl: {})
    monkeypatch.setattr(pos, "_price_history", lambda s: [])
    monkeypatch.setattr(pos, "_last_debate", lambda s: None)


def test_a_documented_but_unheld_name_renders_rather_than_404s(no_holdings):
    """The contract is explicit and it is the right call: a name the slate documents but the broker
    does not hold is exactly what an operator needs to see."""
    body = pos.position("TSM")
    assert body["meta"]["held"] is False
    assert body["live"] is None and body["stop"] is None
    assert body["slate"]["in_slate"] is True
    assert body["slate"]["target_weight_pct"] == 22.0
    assert body["thesis"]["summary"], "an unheld name still shows its case"


def test_a_symbol_nothing_knows_about_is_a_404(no_holdings):
    with pytest.raises(HTTPException) as exc:
        pos.position("ZZZZ")
    assert exc.value.status_code == 404


def test_an_invalid_symbol_is_rejected_before_any_lookup(no_holdings):
    with pytest.raises(HTTPException) as exc:
        pos.position("not a ticker")
    assert exc.value.status_code == 400


def test_a_held_name_with_no_thesis_is_reported_broken(monkeypatch):
    """A held position nobody has written a reason for is broken by definition, whatever the P&L
    says. That is the charter's sell-discipline rule, and the page is built to shout about it."""
    monkeypatch.setattr(pos, "get_snapshot", lambda _p, _acct=None: _snapshot([("MU", 10.0, 100.0)]))
    monkeypatch.setattr(pos, "get_marks", lambda syms, ttl: {"MU": 105.0})
    monkeypatch.setattr(pos, "_price_history", lambda s: [])
    monkeypatch.setattr(pos, "_last_debate", lambda s: None)
    body = pos.position("MU")
    assert body["meta"]["held"] is True
    assert body["slate"]["in_slate"] is False, "MU is not in the documented slate"
    assert body["thesis"]["summary"] is None
    assert body["thesis"]["status"] == "broken"


def test_a_breached_stop_is_reported_broken(monkeypatch):
    monkeypatch.setattr(pos, "get_snapshot", lambda _p, _acct=None: _snapshot([("TSM", 1.0, 100.0)]))
    monkeypatch.setattr(pos, "get_marks", lambda syms, ttl: {"TSM": 70.0})  # -30%
    monkeypatch.setattr(pos, "_price_history", lambda s: [])
    monkeypatch.setattr(pos, "_last_debate", lambda s: None)
    body = pos.position("TSM")
    assert body["stop"]["breached"] is True
    assert body["live"]["unrealized_pl_pct"] == pytest.approx(-30.0)
    assert body["stop"]["distance_to_stop_pct"] == pytest.approx(-10.0)
    assert body["thesis"]["status"] == "broken", "past the stop is broken even with a thesis"


def test_an_unpriced_holding_says_so(monkeypatch):
    """Every number derived from a price is unknown when the mark is missing. Reporting priced=false
    is what stops a blank market value reading as a zero one."""
    monkeypatch.setattr(pos, "get_snapshot", lambda _p, _acct=None: _snapshot([("TSM", 1.0, 100.0)]))
    monkeypatch.setattr(pos, "get_marks", lambda syms, ttl: {"TSM": None})
    monkeypatch.setattr(pos, "_price_history", lambda s: [])
    monkeypatch.setattr(pos, "_last_debate", lambda s: None)
    body = pos.position("TSM")
    assert body["live"]["priced"] is False
    assert body["live"]["market_value"] is None
    assert body["live"]["unrealized_pl_pct"] is None
    assert body["stop"]["breached"] is False, "an unknown price is not a breached stop"


def test_price_history_failure_leaves_an_empty_series_not_a_fabricated_one(monkeypatch):
    """A chart invented to avoid an empty state is a lie told in a shape people trust more than
    text."""
    monkeypatch.setattr(pos, "get_snapshot", lambda _p, _acct=None: _snapshot())
    monkeypatch.setattr(pos, "get_marks", lambda syms, ttl: {})
    monkeypatch.setattr(pos, "_last_debate", lambda s: None)

    def boom(_symbol):
        raise RuntimeError("FMP down")

    monkeypatch.setattr(pos, "_price_history", pos._price_history)
    monkeypatch.setattr("src.fmp.get_shared_client", boom)
    body = pos.position("TSM")
    assert body["price_history"] == []
    assert body["meta"]["price_history_from"] is None


def test_an_unreadable_account_is_503_not_an_empty_page(monkeypatch):
    """'We cannot read the account' and 'you do not hold this' are different answers."""
    from app.services.snapshot import SnapshotError

    def boom(_p, _acct=None):
        raise SnapshotError("broker unreachable")

    monkeypatch.setattr(pos, "get_snapshot", boom)
    with pytest.raises(HTTPException) as exc:
        pos.position("TSM")
    assert exc.value.status_code == 503
