"""§5.5 — recovery-code input folding: Crockford base32 is canonically case-insensitive, and
grouping separators (spaces, hyphens) are presentation, not entropy. The canonical form is what
bin/manage_operator.py hashes and prints (10 uppercase Crockford characters); the service folds
OPERATOR input to it before shape-matching, so a correctly transcribed code never counts as a
failed attempt — five presentation typos must not §5.8-lock an operator out of a live brokerage
view. The end-to-end proof (variants consume the real hashed code against Postgres) lives in
test_auth_db.py; these unit tests pin the folding itself.
"""

from __future__ import annotations

import pytest
from app.services import auth

CANONICAL = "ABCDEFGH01"


@pytest.mark.parametrize(
    "typed",
    [
        "ABCDEFGH01",  # already canonical
        "abcdefgh01",  # lowercase — case-insensitive on decode by Crockford's definition
        "ABCDE-FGH01",  # hyphen grouping off a printed card
        "abcde-fgh01",
        "ABCDE FGH01",  # space grouping
        "  abc-de fgh-01  ",  # every separator at once, plus outer whitespace
    ],
)
def test_variants_fold_to_the_canonical_code(typed: str):
    folded = auth._canonical_recovery_code(typed)
    assert folded == CANONICAL
    assert auth._RECOVERY_CODE_SHAPE.fullmatch(folded)


def test_folding_is_idempotent():
    once = auth._canonical_recovery_code("abcde-fgh01")
    assert auth._canonical_recovery_code(once) == once


def test_folding_cannot_merge_distinct_codes():
    """The separators stripped are outside the code alphabet, and the alphabet has no case
    collisions — so two different canonical codes can never fold to the same value."""
    assert auth._canonical_recovery_code("jkmnpqrstv") != CANONICAL
    assert auth._canonical_recovery_code("JKMN-PQRSTV") == "JKMNPQRSTV"


def test_excluded_letters_stay_excluded():
    """I/L/O/U are not in the alphabet; folding does not (and must not) transliterate them —
    entropy and the printed card's exact alphabet are preserved."""
    assert auth._RECOVERY_CODE_SHAPE.fullmatch(auth._canonical_recovery_code("ILOUILOUIL")) is None


def test_totp_shaped_input_is_untouched_by_recovery_folding():
    """A 6-digit TOTP code folded for the recovery check stays 6 digits — it can never
    accidentally satisfy the 10-character recovery shape."""
    assert auth._canonical_recovery_code("123 456") == "123456"
    assert auth._RECOVERY_CODE_SHAPE.fullmatch("123456") is None
