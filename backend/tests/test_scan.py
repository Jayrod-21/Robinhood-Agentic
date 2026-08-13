"""Scan request guards: ticker-list cap (B3), all-invalid rejection (S4), rate limit (F4).
Plus the result shape: _screen_one must surface the full fundamentals set (issue #27)."""

import logging

import pytest
from app.config import get_settings
from app.ratelimit import debate_limiter, scan_limiter
from app.routers.scan import ScanRequest, _screen_one, run_stream
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


def test_screen_one_surfaces_all_fundamentals(monkeypatch):
    """Issue #27: everything fetch_fundamentals returns must flow into the scan row — market cap,
    price, both P/Es, gross margin, revenue growth, sector, industry — not just PEG/FCF-yield."""
    import src.data

    fake = {
        "ticker": "TSM",
        "market_cap": 2_208_556_122_112.0,
        "peg": 1.38,
        "fcf_yield": 32.5,
        "free_cash_flow": 7.19e11,
        "net_income": 1.9e12,
        "operating_cash_flow": 2.3e12,
        "gross_margin": 0.62,
        "revenue_growth": 0.35,
        "name": "Taiwan Semiconductor",
        "sector": "Technology",
        "industry": "Semiconductors",
        "trailing_pe": 36.55,
        "forward_pe": 21.66,
        "price": 425.83,
    }
    # _screen_one imports fetch_fundamentals at call time, so patching the module attr suffices.
    monkeypatch.setattr(src.data, "fetch_fundamentals", lambda t: dict(fake))

    row = _screen_one("TSM", min_cap=5_000_000_000)

    assert row["ok"] is True
    for key in (
        "market_cap",
        "price",
        "trailing_pe",
        "forward_pe",
        "gross_margin",
        "revenue_growth",
        "name",
        "sector",
        "industry",
    ):
        assert row[key] == fake[key], f"{key} not plumbed through _screen_one"
    # The two gate metrics still come from the screen tiers, not raw passthrough.
    assert row["peg"] is not None
    assert row["fcf_yield"] is not None


def test_screen_one_sparse_fundamentals_yield_none_not_keyerror(monkeypatch):
    """A sparse yfinance payload (fields missing entirely) must produce None values, not blow up —
    the frontend renders None as an em dash."""
    import src.data

    sparse = {"ticker": "XYZ", "market_cap": 6_000_000_000.0}
    monkeypatch.setattr(src.data, "fetch_fundamentals", lambda t: dict(sparse))

    row = _screen_one("XYZ", min_cap=5_000_000_000)

    assert row["ok"] is True
    for key in ("price", "trailing_pe", "forward_pe", "gross_margin", "revenue_growth", "industry"):
        assert row[key] is None


def test_screen_one_no_data_row_shape(monkeypatch):
    """The yfinance-miss row keeps its minimal shape (ok=False + reason) and doesn't crash."""
    import src.data

    monkeypatch.setattr(src.data, "fetch_fundamentals", lambda t: None)
    row = _screen_one("MISS", min_cap=5_000_000_000)
    assert row == {"ticker": "MISS", "ok": False, "passed": False, "reason": "no data (yfinance miss)"}


def test_scan_budget_is_separate_from_debate_budget():
    """A free scan must never draw down the paid-debate cooldown, or vice versa."""
    assert scan_limiter is not debate_limiter
    debate_limiter.reset()
    try:
        assert run_stream(ScanRequest(tickers=["NVDA"])) is not None  # consumes SCAN budget only
        assert debate_limiter.check_and_consume(60) == 0  # debate budget untouched
    finally:
        debate_limiter.reset()
