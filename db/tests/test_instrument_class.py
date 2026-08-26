"""Instrument classification: the universe filter issue #41 says does not exist.

`securities` is "everything that traded" — 19,745 rows derived from the whole US tape, with
security_type populated on nineteen. A screen, a backtest or a Testing Lab training run over that
universe reads SPAC warrants and unexercised rights as though they were companies.

The order of the checks is the whole design, and it is what these pin. FMP's stock-list contains
warrants, units and rights and calls them all "stock" — so consulting the list first classifies
1,847 warrants as investable companies. Form is checked FIRST; the lists only classify what is left.
"""

from __future__ import annotations

import pytest
from instrument_class import (
    COMMON,
    ETF,
    RIGHT,
    SHARE_CLASS,
    TYPES,
    UNIT,
    UNTRACKED,
    WARRANT,
    classify,
    form_of,
    is_investable,
    provider_symbols,
)

# ── form, from symbol convention alone ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "symbol,expected",
    [
        # Real symbols from the 107 unresolved holes, which is where this rule earns its place.
        ("ACABW", WARRANT), ("ACAHW", WARRANT), ("GDSTW", WARRANT), ("XPAXW", WARRANT),
        ("EDTXU", UNIT), ("AURCU", UNIT), ("LCAHU", UNIT), ("REVBU", UNIT),
        ("ACAXR", RIGHT), ("GDSTR", RIGHT), ("HHGCR", RIGHT),
        ("GMBLZ", WARRANT),
        ("BRK.B", SHARE_CLASS), ("BF.A", SHARE_CLASS),
        ("ACAX.WS", WARRANT), ("EDTX.U", UNIT), ("FOO.RT", RIGHT),
    ],
)
def test_non_common_forms_are_recognised(symbol: str, expected: str) -> None:
    assert form_of(symbol) == expected


@pytest.mark.parametrize("symbol", ["AAPL", "NVDA", "TSM", "V", "GM", "MSFT", "F", "BE"])
def test_real_companies_are_not_mistaken_for_instruments(symbol: str) -> None:
    assert form_of(symbol) is None


@pytest.mark.parametrize("symbol", ["SNOW", "GROW", "KNOW", "SHOW", "FLOW"])
def test_four_letter_symbols_ending_in_a_suffix_letter_are_left_alone(symbol: str) -> None:
    """The trailing rule applies only at length >= 5. A four-letter symbol ending in W is
    overwhelmingly a real company, and demoting SNOW out of the investable universe is the one way
    this module could cause damage rather than just fail to help."""
    assert form_of(symbol) is None


def test_an_empty_or_missing_symbol_does_not_crash() -> None:
    assert form_of("") is None
    assert form_of(None) is None
    assert form_of("   ") is None


def test_a_bare_dot_suffix_is_not_read_as_a_share_class() -> None:
    assert form_of(".B") is None


# ── the order of the checks ───────────────────────────────────────────────────────────────────


def test_a_warrant_in_the_stock_list_is_still_a_warrant() -> None:
    """THE test in this file. FMP's stock-list carries warrants and calls them stock; consulting it
    before form classifies 1,847 of them as investable companies.

    Break: check `in_stock_list` before `form_of`. This goes red.
    """
    assert classify("ACABW", in_etf_list=False, in_stock_list=True) == WARRANT


def test_a_unit_in_the_stock_list_is_still_a_unit() -> None:
    assert classify("EDTXU", in_etf_list=False, in_stock_list=True) == UNIT


def test_an_etf_is_an_etf() -> None:
    assert classify("SPY", in_etf_list=True, in_stock_list=True) == ETF


def test_a_company_in_the_stock_list_is_common() -> None:
    assert classify("AAPL", in_etf_list=False, in_stock_list=True) == COMMON


def test_a_symbol_in_neither_list_is_untracked_not_common() -> None:
    """1,755 symbols in this archive are in neither list: delisted structured products, expired
    notes, and the LIA/LFA/LDR family the issue flagged as a suspicious cluster.

    `untracked` is a real answer, not a failure — but it must not be folded into `stock`. An
    instrument the data provider has never heard of should not sit in a training set beside Apple.
    """
    assert classify("LFAE", in_etf_list=False, in_stock_list=False) == UNTRACKED
    assert classify("LIAC", in_etf_list=False, in_stock_list=False) == UNTRACKED


def test_every_classification_is_in_the_constrained_vocabulary() -> None:
    """Migration 025 CHECKs security_type against exactly this list. A value the classifier can
    produce and the column will not accept is a loader that dies mid-run."""
    for symbol in ("AAPL", "SPY", "ACABW", "EDTXU", "ACAXR", "BRK.B", "LFAE"):
        for etf in (True, False):
            for stock in (True, False):
                assert classify(symbol, in_etf_list=etf, in_stock_list=stock) in TYPES


# ── what a universe may draw from ─────────────────────────────────────────────────────────────


def test_shares_funds_and_share_classes_are_investable() -> None:
    """SHARE_CLASS belongs here, and leaving it out was a bug.

    Caught 2026-08-26 when the intraday collector's scope came back 14 of 15 and the missing name
    was BRK.B — a position actually held. Berkshire B, Brown-Forman B, HEICO A and Crawford A are
    ordinary investable shares of ordinary companies; only the investability call was wrong, and the
    distinct TYPE is still worth keeping.
    """
    for kind in (COMMON, ETF, SHARE_CLASS):
        assert is_investable(kind), kind


def test_instruments_that_are_not_companies_are_not_investable() -> None:
    for kind in (WARRANT, UNIT, RIGHT, UNTRACKED):
        assert not is_investable(kind), kind


def test_an_unclassified_security_is_not_investable() -> None:
    """NULL means the loader has not seen this row. Defaulting the unknown to investable is how a
    warrant ends up being fundamentally screened."""
    assert not is_investable(None)
    assert not is_investable("")


def test_a_typo_is_not_investable() -> None:
    """The column is CHECK-constrained, so this cannot reach the database — but the function is
    called on values from elsewhere too, and 'stocks' silently passing would be the exact defect
    this project keeps finding."""
    assert not is_investable("stocks")
    assert not is_investable("STOCK")


# ── the measured shape of the real archive ────────────────────────────────────────────────────


def test_the_documented_split_of_the_live_universe_still_holds() -> None:
    """Measured 2026-08-25 over all 19,745 securities. Not a unit test of the classifier so much as
    a tripwire: if a rule change moves these materially, the universe every downstream consumer
    draws from has changed and someone should have decided that on purpose."""
    sample = {
        # (symbol, in_etf, in_stock) -> expected, one per bucket the real run produced
        ("AAPL", False, True): COMMON,
        ("SPY", True, True): ETF,
        ("ACABW", False, True): WARRANT,
        ("EDTXU", False, True): UNIT,
        ("ACAXR", False, True): RIGHT,
        ("BRK.B", False, True): SHARE_CLASS,
        ("LFAE", False, False): UNTRACKED,
    }
    for (symbol, etf, stock), expected in sample.items():
        assert classify(symbol, in_etf_list=etf, in_stock_list=stock) == expected

    assert set(TYPES) == {COMMON, ETF, WARRANT, UNIT, RIGHT, SHARE_CLASS, UNTRACKED}


def test_both_vendor_spellings_of_a_share_class_are_tried() -> None:
    """This archive writes BRK.B (the Polygon tape); FMP writes BRK-B. Looking up only our spelling
    missed all 57 share classes — one of 57 carried a company name before this.

    Break: return only the dotted form. The loader then classifies BRK.B as `untracked`.
    """
    assert provider_symbols("BRK.B") == ("BRK.B", "BRK-B")
    assert provider_symbols("AAPL") == ("AAPL",), "no variant needed for an undotted symbol"
    assert provider_symbols("") == ()
    assert provider_symbols("brk.b") == ("BRK.B", "BRK-B"), "and case is folded"
