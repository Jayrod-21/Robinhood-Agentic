"""Shared validation primitives: ticker normalization and record-id safety (B1 first line)."""

import pytest
from fastapi import HTTPException

from app.validation import is_safe_record_id, normalize_ticker, validate_ticker


@pytest.mark.parametrize("raw,expected", [("nvda", "NVDA"), (" oxy ", "OXY"), ("brk.b", "BRK.B")])
def test_normalize_ticker_ok(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", ["123", "!!", "toolongggg", "", "  "])
def test_normalize_ticker_rejects(raw):
    assert normalize_ticker(raw) is None


def test_validate_ticker_raises_on_bad():
    with pytest.raises(HTTPException) as exc:
        validate_ticker("123")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("rid", ["2026-06-16-engine-NVDA", "abc_123", "a.b-c", "x"])
def test_is_safe_record_id_accepts(rid):
    assert is_safe_record_id(rid) is True


@pytest.mark.parametrize(
    "rid",
    ["..", "../x", "a/b", "a\\b", "..%2F..", "a/../b", "", "x" * 81, "a b", "a;b"],
)
def test_is_safe_record_id_rejects(rid):
    assert is_safe_record_id(rid) is False
