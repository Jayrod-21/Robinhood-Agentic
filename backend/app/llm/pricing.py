"""What a call costs, as a VERSIONED assumption rather than a fact.

WHY THIS IS SEPARATE FROM THE USAGE RECORD
    Tokens are a fact: the provider reported them and they never change. A dollar cost is an
    assumption — it depends on a price list that moves, and that this project reads from
    documentation rather than from the provider's billing API.

    So intraday_observations' lesson applies here too (#133): store the fact, derive the assumption,
    and version the derivation. A row records the tokens and the PRICING_VERSION that priced them.
    When a price changes, or when one of these numbers turns out to be wrong, every affected row can
    be found and repriced — instead of leaving a ledger that is quietly incorrect in a way nobody
    can locate.

    That matters more here than usual, because this ledger decides who owes whom.

SOURCE AND STALENESS
    Rates below are Anthropic's first-party API prices as documented on 2026-06-24. They are NOT
    read from a billing API, so they can drift without anything here noticing. Treat a cost as
    accurate to the price list, not to the invoice — and reconcile against the real bill before
    settling up.
"""

from __future__ import annotations

from decimal import Decimal

# Bump whenever a rate below changes, or when the arithmetic does. A version change means "rows
# priced under N and rows priced under N+1 are not comparable until the older ones are repriced".
PRICING_VERSION = 1

# USD per 1,000,000 tokens. (input, output).
_RATES: dict[str, tuple[Decimal, Decimal]] = {
    # Anthropic — documented 2026-06-24.
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-fable-5": (Decimal("10.00"), Decimal("50.00")),
}

_PER_MILLION = Decimal(1_000_000)


class UnknownModel(KeyError):
    """No published rate for this model. The caller decides what to do; this does not guess."""


def rate_for(model: str) -> tuple[Decimal, Decimal] | None:
    """(input, output) USD per million tokens, or None when the model is not priced here."""
    return _RATES.get((model or "").strip())


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Estimated cost, or None for an unpriced model.

    None rather than zero, deliberately. A zero cost is a claim that the call was free; None is the
    truthful "we do not know what this cost". A ledger that silently prices unknown models at zero
    would under-report exactly the spend nobody is watching — a new model dropped into JURY_MODEL,
    for instance.
    """
    rate = rate_for(model)
    if rate is None:
        return None
    per_in, per_out = rate
    return (
        (Decimal(max(0, input_tokens)) * per_in + Decimal(max(0, output_tokens)) * per_out)
        / _PER_MILLION
    )


def priced_models() -> tuple[str, ...]:
    """Every model this version can price. Surfaced so an unpriced model is diagnosable."""
    return tuple(sorted(_RATES))
