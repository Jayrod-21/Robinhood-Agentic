"""Shared input-validation primitives for the API's external string inputs.

Every endpoint that accepts user-controlled text routes it through one of these helpers so the
contract cannot drift between routers (the duplicated ``TICKER_RE`` copies had already started to
diverge — one router raised via a helper, another inlined the check). Centralizing them also makes
the security boundary auditable in one place: a ticker is an uppercase symbol, a record id is a safe
filename stem, and nothing else reaches yfinance or the filesystem.
"""

from __future__ import annotations

import re

from fastapi import HTTPException

# An equity ticker: 1–5 letters, optionally a single class-share suffix of ".<letter>" (NVDA,
# BRK.B, BF.B). Exactly this grammar — no consecutive dots, no trailing dot, at most one dot —
# because the symbol is interpolated into an outbound Yahoo URL path by yfinance.
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

# A debate record id used as a filename stem. Allowlist of filename-safe characters only; crucially
# excludes "/" and prevents ".." traversal (no path separators can appear, and a bare ".." is < the
# min useful length anyway but is also explicitly rejected by the resolved-path containment check in
# records.get_record). 80-char cap matches the longest archive stems.
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def normalize_ticker(raw: str) -> str | None:
    """Upper-case + strip a ticker, returning the clean symbol or None if it fails the contract."""
    ticker = raw.strip().upper()
    return ticker if TICKER_RE.match(ticker) else None


def validate_ticker(raw: str) -> str:
    """Return the normalized ticker or raise HTTP 400. The single guard for paid/scan ticker input."""
    ticker = normalize_ticker(raw)
    if ticker is None:
        raise HTTPException(status_code=400, detail=f"Invalid ticker: {raw!r}")
    return ticker


def is_safe_record_id(record_id: str) -> bool:
    """True only for filename-safe ids: no path separators, no ``..`` traversal, bounded length.

    This is the first line of defense for ``get_record`` (the second is resolved-path containment).
    Rejecting ``..`` and "/" here means a percent-encoded ``../`` in the URL — which Starlette decodes
    into ``record_id`` AFTER routing — never reaches the filesystem join.
    """
    if ".." in record_id or "/" in record_id or "\\" in record_id:
        return False
    return bool(RECORD_ID_RE.match(record_id))
