"""Which API key to use for the next call, and whose bill it lands on.

THE PROBLEM
    Two owners, one system. Measured 2026-08-26: Jared had spent roughly $9 on Anthropic and Joe
    nothing. The goal is to SPLIT that going forward.

WHY NOT ROUND-ROBIN
    Alternating keys 50/50 from today leaves the existing $9 gap in place permanently — it freezes
    the imbalance rather than correcting it. To actually converge, selection has to prefer the owner
    who is BEHIND, which means knowing what each has spent. That is what llm_usage records and what
    `select` reads.

    The gap closes on its own and then the split becomes approximately even, with no manual
    intervention and no scheduled "your turn" bookkeeping.

OWNERSHIP IS NOT POSITIONAL — READ THIS BEFORE ADDING A PROVIDER
    The environment does NOT follow "_1 is always the same person":

        ANTHROPIC_API_KEY   = Jared      ANTHROPIC_API_KEY_2 = Joe
        GEMINI_API_KEY      = JOE        GEMINI_API_KEY_2    = JARED

    Anything that assumes slot 1 belongs to one person will misattribute every Gemini call, and a
    cost ledger that misattributes is worse than no ledger — it produces a confident wrong answer
    about who owes whom. Owners come from the *_NAME variables, never from the slot number.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("agentic.llm.keys")

ANTHROPIC = "anthropic"
GEMINI = "gemini"

# (provider, key env var, owner-label env var). Slot order carries NO ownership meaning.
_SLOTS: tuple[tuple[str, str, str], ...] = (
    (ANTHROPIC, "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_NAME"),
    (ANTHROPIC, "ANTHROPIC_API_KEY_2", "ANTHROPIC_API_KEY_NAME_2"),
    (GEMINI, "GEMINI_API_KEY", "GEMINI_API_KEY_NAME"),
    (GEMINI, "GEMINI_API_KEY_2", "GEMINI_API_KEY_NAME_2"),
)


@dataclass(frozen=True)
class ApiKey:
    provider: str
    owner: str
    secret: str

    def describe(self) -> dict[str, str]:
        """Never the secret. Not even a prefix — a key id is half a credential, and a page that
        shows one teaches an operator it is safe to paste."""
        return {"provider": self.provider, "owner": self.owner}


def _env(name: str) -> str:
    # Stripped: the labels arrived with a leading space, and an unstripped owner name would create
    # " Jared Anthropic" and "Jared Anthropic" as two distinct owners in the ledger.
    return (os.environ.get(name) or "").strip()


def available(provider: str | None = None) -> list[ApiKey]:
    """Every configured key, optionally for one provider. Order is slot order, not preference."""
    keys: list[ApiKey] = []
    for slot_provider, secret_var, name_var in _SLOTS:
        if provider and slot_provider != provider:
            continue
        secret = _env(secret_var)
        if not secret:
            continue
        owner = _env(name_var)
        if not owner:
            # A key with no owner label still WORKS, but its spend cannot be attributed — so it is
            # named after the variable rather than silently pooled with someone else's total.
            logger.warning("%s has no %s; attributing its spend to the variable name", secret_var, name_var)
            owner = secret_var
        keys.append(ApiKey(provider=slot_provider, owner=owner, secret=secret))
    return keys


def select(provider: str, spend_by_owner: dict[str, float] | None = None) -> ApiKey | None:
    """The key to use next: the configured owner with the least spend so far.

    `spend_by_owner` is estimated USD keyed by owner label. An owner absent from it has spent
    nothing recorded, which sorts them first — correct, and the reason a newly-added key absorbs
    calls until it catches up.

    Ties break on slot order so the choice is deterministic; a random tiebreak would make the same
    inputs produce different bills and make this impossible to reason about.
    """
    keys = available(provider)
    if not keys:
        return None
    spend = spend_by_owner or {}
    return min(
        enumerate(keys), key=lambda pair: (float(spend.get(pair[1].owner, 0.0)), pair[0])
    )[1]
