"""Scan request guards: ticker-list cap (B3) and all-invalid rejection (S4)."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import get_settings
from app.routers.scan import ScanRequest, run_stream


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
