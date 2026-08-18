"""Guards on db/load_fundamentals.py — the point-in-time rules that make history trustworthy.

The loader is where a vendor's bad record becomes a permanent claim in a table other things query
by date. These tests are about the two ways that goes wrong: a filing dated before the period it
reports on, and a historical row wearing today's market figures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))

from load_fundamentals import _filing_is_plausible, annual_row


def test_a_filing_dated_before_its_period_ended_is_implausible():
    """FMP ships UNH FY2023 as accepted 2023-12-29 for a period ending 2023-12-31 — a 10-K filed
    two days before the year it reports on closed. Accepting that would make the period look
    knowable before it existed, which is exactly what known_at is queried to prevent."""
    assert _filing_is_plausible("2023-12-31", "2023-12-29T19:00:00Z") is False
    assert _filing_is_plausible("2023-12-31", "2024-02-28T10:00:00Z") is True
    # Same day is fine: a period can be reported on the day it ends.
    assert _filing_is_plausible("2023-12-31", "2023-12-31T23:59:59Z") is True
    assert _filing_is_plausible("2023-12-31", "gibberish") is False


def _bundle(period_end: str, accepted: str) -> dict:
    statement = {"date": period_end, "acceptedDate": accepted, "revenue": 100.0, "ebitda": 20.0}
    return {
        "income": statement,
        "profile": {"beta": 1.2, "range": "10-20", "averageVolume": 5_000_000, "price": 15.0},
        "periods": {"income": [statement, statement]},
    }


def test_an_implausible_period_is_dropped_rather_than_dated_with_a_guess(caplog):
    """Clamping the date to the period end would invent a knowability claim. Dropping the period
    costs one year of history; a wrong known_at silently leaks the future into every point-in-time
    query built on the table."""
    with caplog.at_level("WARNING"):
        row = annual_row(_bundle("2023-12-31", "2023-12-29 14:00:00"), security_id=1, index=0)
    assert row is None
    assert any("precedes the period end" in r.getMessage() for r in caplog.records), (
        "a dropped period must be named — a silently shorter history is indistinguishable from a "
        "company that simply has not filed for that long"
    )


def test_a_plausible_period_still_produces_a_row():
    """Guards the test above: if the fixture shape drifted so nothing parsed, asserting None would
    pass for the wrong reason."""
    row = annual_row(_bundle("2023-12-31", "2024-02-28 14:00:00"), security_id=1, index=0)
    assert row is not None
    assert row["period_end"] == "2023-12-31"


def test_a_historical_period_does_not_wear_todays_market_figures():
    """Beta, the 52-week range and average volume come from the PROFILE, which describes today.
    Attaching them to a period that closed years ago would be a fabricated historical fact — the
    most tempting mistake in a history table, because the columns exist and the data is right
    there."""
    bundle = _bundle("2023-12-31", "2024-02-28 14:00:00")
    newest = annual_row(bundle, security_id=1, index=0)
    older = annual_row(bundle, security_id=1, index=1)
    assert newest is not None and older is not None
    assert newest["beta"] == pytest.approx(1.2), "the newest row may carry today's profile"
    for field in ("beta", "week_52_high", "week_52_low", "avg_volume_30d"):
        assert older[field] is None, f"{field} describes today and must not be dated to a past period"
