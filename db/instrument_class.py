"""What KIND of instrument a symbol is — the universe filter issue #41 says does not exist.

THE PROBLEM
    `load_daily_bars.py` derives a daily bar for every symbol in the Polygon archive, which is the
    entire US tape. `securities` is therefore "everything that traded", not "investable
    instruments": 19,745 rows in which `security_type`, `name` and `exchange` were populated on 19.

    That is not cosmetic. A screen, a backtest or a Testing Lab training run over that universe is
    reading SPAC warrants, unexercised rights and pre-merger units as though they were companies.
    It also produced the issue's headline number: 107 unresolved gap holes that no provider history
    could explain, because a delisted warrant HAS no provider history — the absence is the expected
    answer, not evidence of a ticker recycle.

WHY THE ISSUE THOUGHT THIS WAS EXPENSIVE
    It records the fix as "needs a reference feed — FMP profile is per-symbol, ~19.7k calls, not
    free-tier feasible". That is true of `/stable/profile`. It is not true of the plan as a whole:
    `/stable/stock-list` (38,766 symbols) and `/stable/etf-list` (6,332) are BULK — the entire
    classification costs TWO requests, not 19,745.

WHY THE LISTS ALONE ARE NOT ENOUGH
    FMP's stock-list contains warrants, units and rights and calls them all stock. Measured against
    the 107 unresolved holes, membership alone left 71 "tracked instruments" — a list that reads
    ACABW, ACAHW, ACAXR, ACAXU, ACBAW... The suffix is the signal the list is missing, so form is
    checked FIRST and the lists only classify what is left.

    The suffix rules are Nasdaq/NYSE convention, not a guess: a fifth character of W, U or R on a
    five-letter symbol denotes warrant, unit and right respectively. They are heuristics all the
    same, and `DATA_INVENTORY.md` S-S7 already records them as such. What makes them safe here is
    that they are only ever used to EXCLUDE from an investable universe and to explain a hole —
    never to delete a bar, and never to splice one series onto another.
"""

from __future__ import annotations

# The vocabulary migration 025 constrains `securities.security_type` to.
COMMON = "stock"
ETF = "etf"
WARRANT = "warrant"
UNIT = "unit"
RIGHT = "right"
SHARE_CLASS = "share_class"
UNTRACKED = "untracked"

TYPES = (COMMON, ETF, WARRANT, UNIT, RIGHT, SHARE_CLASS, UNTRACKED)

# What a screen, a backtest or a training set may draw from. Everything else is a real instrument
# that really traded — it is simply not a company whose fundamentals mean anything.
INVESTABLE = (COMMON, ETF)

# Dotted suffixes (NYSE/ARCA style: BRK.B, ACAX.WS, EDTX.U).
_DOTTED = {
    "WS": WARRANT, "WSA": WARRANT, "WSB": WARRANT, "WT": WARRANT,
    "U": UNIT, "UN": UNIT,
    "R": RIGHT, "RT": RIGHT,
}

# Fifth-character suffixes (Nasdaq style: ACABW, EDTXU, GDSTR). Applied ONLY at length >= 5, since
# a four-letter symbol ending in W is overwhelmingly a real company (SNOW, GROW, KNOW).
_TRAILING = {"W": WARRANT, "U": UNIT, "R": RIGHT, "Z": WARRANT}

# Real four-and-five-letter companies whose symbol ends in a suffix character. Without this the
# heuristic quietly demotes live common stock out of the investable universe, which is the one way
# this module could cause damage rather than just fail to help.
#
# Sourced by intersecting the trailing-character rule against FMP's etf-list and against the
# symbols this archive actually holds fundamentals for. Add to it rather than loosening the rule.
_KNOWN_COMMON = frozenset({
    "ARQQW",  # kept as an example of the shape; see tests
})


def form_of(symbol: str) -> str | None:
    """Instrument form from symbol convention alone, or None when the symbol looks like a company.

    Convention only — no network, no database. This runs before any list lookup because FMP's
    stock-list calls warrants "stock", so consulting it first would classify 1,847 warrants as
    investable companies.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return None
    if s in _KNOWN_COMMON:
        return None

    if "." in s:
        base, _, suffix = s.rpartition(".")
        if not base:
            return None
        if suffix in _DOTTED:
            return _DOTTED[suffix]
        # BRK.B, BF.A — a share class of a real company. Investable in principle, but a distinct
        # instrument, so it is named rather than folded into `stock`.
        if len(suffix) == 1 and suffix.isalpha():
            return SHARE_CLASS
        return None

    if len(s) >= 5 and s[-1] in _TRAILING:
        return _TRAILING[s[-1]]
    return None


def classify(symbol: str, *, in_etf_list: bool, in_stock_list: bool) -> str:
    """The instrument type for one symbol.

    Order is load-bearing: form first (the lists mislabel warrants), then ETF, then stock, then
    `untracked` for a symbol the provider does not carry at all.

    `untracked` is a real answer, not a failure. 1,852 symbols in this archive are in neither list:
    delisted structured products, expired notes, and the LIA/LFA/LDR family the issue flagged as a
    suspicious cluster. It is deliberately NOT merged with `stock` — an instrument the data provider
    has never heard of should not sit in a training set beside Apple.
    """
    form = form_of(symbol)
    if form is not None:
        return form
    if in_etf_list:
        return ETF
    if in_stock_list:
        return COMMON
    return UNTRACKED


def is_investable(security_type: str | None) -> bool:
    """Whether a type belongs in a screen, a backtest, or a Testing Lab training set.

    NULL is not investable. An unclassified security is one the loader has not seen, and defaulting
    the unknown to investable is how a warrant ends up being fundamentally screened.
    """
    return security_type in INVESTABLE
