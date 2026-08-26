"""GET /api/intraday — the read path the collector never had.

#135 built the tables, the arithmetic and the cron and stopped at the database, so the series
accumulated with nothing able to look at it. The frontend could not have rendered it even in
principle. These pin the shape Joe builds against, and the three-way ambiguity it has to resolve:
an empty chart must distinguish "the market is closed" from "the collector died" from "this symbol
left the watchlist".
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from app.db import DbUnavailable
from app.routers import intraday as mod
from fastapi import HTTPException

AT = datetime(2026, 8, 26, 16, 30, tzinfo=timezone.utc)


class _Conn:
    """Returns canned rows per query, matched on a fragment of the SQL."""

    def __init__(self, dates, rows, run):
        self._dates, self._rows, self._run = dates, rows, run

    def execute(self, sql, _params=None):
        self._last = sql
        return self

    def fetchall(self):
        return self._dates if "DISTINCT session_date" in self._last else self._rows

    def fetchone(self):
        return self._run

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _row(symbol="NVDA", *, pe=None, fcf=Decimal("0.019"), lineage=True, reasons=("held",)):
    return (
        symbol, AT, AT.date(), Decimal("210.22"), Decimal("5091946920600"), 48_236_666,
        pe, None, fcf, list(reasons), lineage, 1,
    )


# Module-level singletons rather than call-in-default (ruff B008): a mutable-ish default evaluated
# once at import is exactly the footgun that rule exists for, even in a fixture.
_DEFAULT_DATES = ((AT.date(),),)
_DEFAULT_RUN = (AT, "complete", 15, 15, 0, None)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    def _install(dates=_DEFAULT_DATES, rows=None, run=_DEFAULT_RUN):
        rows = [_row()] if rows is None else rows
        monkeypatch.setattr(mod, "connection", lambda: _Conn(list(dates), list(rows), run))
        return mod.intraday

    return _install


# ── the series ────────────────────────────────────────────────────────────────────────────────


def test_an_observation_carries_price_ratios_and_scope(api) -> None:
    body = api()()
    point = body["observations"][0]

    assert point["symbol"] == "NVDA"
    assert point["price"] == 210.22
    assert point["fcf_yield"] == 0.019
    assert point["scope_reasons"] == ["held"]
    assert point["formula_version"] == 1


def test_numerics_are_floats_not_decimals(api) -> None:
    """Decimal is not JSON-serialisable, and a router that only ever ran against an empty table
    would not find that out."""
    point = api()()["observations"][0]

    for field in ("price", "market_cap", "fcf_yield"):
        assert isinstance(point[field], float), field


def test_a_null_ratio_stays_null_rather_than_becoming_zero(api) -> None:
    """pe_trailing is NULL wherever the in-effect filing carried no EPS — and pe_forward is NULL
    everywhere today, since eps_next_year_est is populated on 0 of 152 rows. Zero would read as a
    P/E of zero, which is a very different claim."""
    point = api()()["observations"][0]

    assert point["pe_trailing"] is None
    assert point["pe_forward"] is None


def test_lineage_is_reported_so_a_null_ratio_can_be_explained(api) -> None:
    """A NULL ratio WITH lineage means the filing did not carry that figure; WITHOUT lineage it
    means there was no filing to read. The page should be able to say which."""
    assert api(rows=[_row(lineage=True)])()["observations"][0]["has_lineage"] is True
    assert api(rows=[_row("GLD", lineage=False, fcf=None)])()["observations"][0]["has_lineage"] is False


# ── the collector's liveness travels with the data ────────────────────────────────────────────


def test_the_last_run_is_reported_beside_the_series(api) -> None:
    """Without it, an empty chart is ambiguous between "the market is closed", "the collector died"
    and "this symbol left the watchlist" — the three-way ambiguity the runs table exists to
    resolve, carried through to the API rather than left in the database."""
    meta = api()()["meta"]["last_run"]

    assert meta["status"] == "complete"
    assert (meta["observed"], meta["failed"], meta["scope_size"]) == (15, 0, 15)


def test_a_skipped_run_carries_its_reason(api) -> None:
    """"market closed" is the most common reason this series has a gap, and it must not read as a
    fault."""
    run = (AT, "skipped", 0, 0, 0, "market closed (no trading session on this date)")
    assert "market closed" in api(run=run)()["meta"]["last_run"]["error"]


def test_a_partly_failed_sweep_is_visible(api) -> None:
    """A sweep that quietly recorded 3 of 15 must not look like a sweep."""
    meta = api(run=(AT, "complete", 15, 3, 12, None))()["meta"]["last_run"]

    assert (meta["observed"], meta["failed"]) == (3, 12)


# ── empty and degenerate states ───────────────────────────────────────────────────────────────


def test_no_observations_at_all_is_a_state_not_an_error(api) -> None:
    """The collector may simply never have run. That is reportable, not a 500."""
    body = api(dates=[])()

    assert body["observations"] == []
    assert body["meta"]["points"] == 0
    assert "note" in body["meta"]


def test_an_unavailable_database_is_503(api, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise DbUnavailable("unreachable", "no pool")

    monkeypatch.setattr(mod, "connection", _boom)
    with pytest.raises(HTTPException) as exc:
        mod.intraday()
    assert exc.value.status_code == 503


def test_truncation_is_declared_rather_than_silent(api) -> None:
    """A caller that asked for 1 point and got 1 must be able to tell a complete answer from a
    clipped one — silent truncation reads as "that is all there is"."""
    body = api(rows=[_row(), _row("AMD")])(limit=1)

    assert body["meta"]["truncated"] is True


def test_a_symbol_filter_is_reported_back(api) -> None:
    body = api()(symbol="nvda")
    assert body["meta"]["symbol"] == "NVDA", "upper-cased, matching how the table stores it"


def test_sessions_count_trading_days_not_calendar_days(api) -> None:
    """Asking for 5 sessions over a holiday week should return five sessions of data, not three.
    The window is derived from the session_dates actually present."""
    dates = [(AT.date(),), (AT.date().replace(day=25),), (AT.date().replace(day=24),)]
    body = api(dates=dates)(sessions=3)

    assert len(body["meta"]["sessions"]) == 3
