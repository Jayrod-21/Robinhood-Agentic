"""Scan request guards: ticker-list cap (B3), all-invalid rejection (S4), rate limit (F4)."""

import logging

import pytest
from app.config import get_settings
from app.ratelimit import debate_limiter, scan_limiter
from app.routers.scan import ScanRequest, run_stream
from fastapi import HTTPException
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _fresh_scan_limiter():
    """Each test starts outside the scan cooldown window and leaves the gate clean."""
    scan_limiter.reset()
    yield
    scan_limiter.reset()


def test_oversized_ticker_list_rejected():
    """A list above scan_max_tickers must be rejected with a 400, not fanned out."""
    cap = get_settings().scan_max_tickers
    # A clearly-too-large list of valid-format tickers.
    req = ScanRequest(tickers=["AAA"] * (cap + 1))
    with pytest.raises(HTTPException) as exc:
        run_stream(req)
    assert exc.value.status_code == 400
    assert "Too many tickers" in exc.value.detail


def test_absurd_list_rejected_by_pydantic():
    """The static pydantic ceiling rejects a pathological multi-hundred-element body (422-class)."""
    with pytest.raises(ValidationError):
        ScanRequest(tickers=["AAA"] * 5000)


def test_all_invalid_tickers_rejected():
    """A request of only malformed tickers is rejected (S4) rather than silently producing a no-op."""
    req = ScanRequest(tickers=["123", "!!", "toolongggg"])
    with pytest.raises(HTTPException) as exc:
        run_stream(req)
    assert exc.value.status_code == 400
    assert "No valid tickers" in exc.value.detail


def test_valid_subset_accepted(monkeypatch):
    """A mix of valid + invalid keeps the valid ones and starts a stream (no raise)."""
    # Avoid any real yfinance work: the handler only builds the generator here, it doesn't iterate.
    req = ScanRequest(tickers=["NVDA", "123", "OXY"])
    resp = run_stream(req)  # returns a StreamingResponse; should not raise
    assert resp is not None


def test_second_scan_inside_cooldown_is_429_and_logged(caplog):
    """F4 regression: back-to-back scans must be rate limited. The first request is admitted; an
    immediate second one gets a 429 that says how long to wait, AND a server-side warning that says
    why — the gate never blocks silently."""
    assert run_stream(ScanRequest(tickers=["NVDA"])) is not None  # admitted, consumes the budget
    with caplog.at_level(logging.WARNING, logger="agentic.routers.scan"):
        with pytest.raises(HTTPException) as exc:
            run_stream(ScanRequest(tickers=["NVDA"]))
    assert exc.value.status_code == 429
    assert "wait" in exc.value.detail
    assert any("scan rate limit hit" in rec.getMessage() for rec in caplog.records)


def test_rejected_request_does_not_consume_scan_budget():
    """A malformed request (400) must not burn the cooldown a valid request then needs."""
    with pytest.raises(HTTPException):
        run_stream(ScanRequest(tickers=["123", "!!"]))  # all invalid → 400, before the gate
    assert run_stream(ScanRequest(tickers=["NVDA"])) is not None  # still admitted


def test_scan_budget_is_separate_from_debate_budget():
    """A free scan must never draw down the paid-debate cooldown, or vice versa."""
    assert scan_limiter is not debate_limiter
    debate_limiter.reset()
    try:
        assert run_stream(ScanRequest(tickers=["NVDA"])) is not None  # consumes SCAN budget only
        assert debate_limiter.check_and_consume(60) == 0  # debate budget untouched
    finally:
        debate_limiter.reset()
