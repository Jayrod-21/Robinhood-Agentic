"""Shared cooldown limiter — the single token-spend budget for debate + pipeline (B2)."""

import time

from app.ratelimit import CooldownLimiter, debate_limiter


def test_first_call_admitted_then_blocked():
    lim = CooldownLimiter()
    assert lim.check_and_consume(60) == 0  # first call passes and stamps the clock
    wait = lim.check_and_consume(60)
    assert wait > 0  # immediate second call is inside the window
    assert wait <= 61


def test_zero_interval_disables_gate():
    lim = CooldownLimiter()
    assert lim.check_and_consume(0) == 0
    assert lim.check_and_consume(0) == 0  # never blocks when disabled


def test_window_clears_after_interval():
    lim = CooldownLimiter()
    assert lim.check_and_consume(0.05) == 0
    time.sleep(0.06)
    assert lim.check_and_consume(0.05) == 0  # interval elapsed → admitted again


def test_debate_and_pipeline_share_one_budget():
    """The key B2 property: debate.py and pipeline.py import the SAME limiter instance, so a debate
    run consumes the budget the pipeline endpoint also checks (and vice versa) — not two budgets."""
    from app.routers import debate as debate_mod
    from app.routers import pipeline as pipeline_mod

    assert debate_mod.debate_limiter is pipeline_mod.debate_limiter
    assert debate_mod.debate_limiter is debate_limiter

    debate_limiter.reset()
    assert debate_limiter.check_and_consume(60) == 0   # "debate" consumes the budget
    assert debate_limiter.check_and_consume(60) > 0    # "pipeline" is now blocked by the same gate
    debate_limiter.reset()
