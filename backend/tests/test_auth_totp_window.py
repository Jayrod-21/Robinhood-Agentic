"""§5.4: the TOTP acceptance window is EXACTLY ±1 step, pinned as a constant, and the matcher
returns the step so the caller can enforce the monotonic replay guard.

Pure unit tests — the clock is injected (``_time``), never slept on.
"""

from __future__ import annotations

import pyotp
import pytest
from app.services import auth

# Mid-step so an off-by-one in the floor division cannot straddle a boundary: step center of an
# arbitrary fixed step (step 63_333_333).
FIXED_NOW = 63_333_333 * 30 + 15
CURRENT_STEP = FIXED_NOW // 30


@pytest.fixture()
def secret(monkeypatch) -> str:
    monkeypatch.setattr(auth, "_time", lambda: float(FIXED_NOW))
    return pyotp.random_base32()


def _code_at(secret: str, step: int) -> str:
    return pyotp.TOTP(secret).at(step * 30)


def test_window_is_pinned_at_one_step():
    # Config-driving this number is the §5.4 brute-force amplifier; widening it must be a code
    # change that fails this test and forces a review.
    assert auth.TOTP_STEP_WINDOW == 1
    assert auth.TOTP_STEP_SECONDS == 30


def test_codes_within_one_step_match_and_report_their_step(secret):
    for offset in (-1, 0, 1):
        step = CURRENT_STEP + offset
        assert auth._match_totp_step(secret, _code_at(secret, step)) == step


def test_codes_two_steps_out_are_rejected(secret):
    for offset in (-2, 2):
        assert auth._match_totp_step(secret, _code_at(secret, CURRENT_STEP + offset)) is None


def test_matcher_returns_step_enabling_the_monotonic_guard(secret):
    """The step −1 code matches step −1 — NOT the current step. The caller's
    ``step > last_used_step`` check is what stops walking backwards; this pins the half the
    matcher owns: it must never report a stale code as current."""
    stale = auth._match_totp_step(secret, _code_at(secret, CURRENT_STEP - 1))
    assert stale == CURRENT_STEP - 1
    assert stale < CURRENT_STEP


def test_garbage_codes_never_match(secret):
    for junk in ("", "12345", "1234567", "abcdef", "12345a"):
        assert auth._match_totp_step(secret, junk) is None
