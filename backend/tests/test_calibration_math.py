"""The reliability statistics, against cases whose answers are known by hand.

These matter more than most: the numbers here become the basis for deciding which agents to trust,
and both ECE and Brier are easy to compute plausibly and wrongly. Every case below has an answer
that can be checked without running the code.
"""

from __future__ import annotations

import pytest
from app.routers.performance import MIN_N_FOR_CALIBRATION, _bin_index, _summarise


def test_a_perfect_forecaster_has_zero_error():
    """Claims 100% and is always right; claims 0% and is never right. Nothing to be sorry for."""
    rows = [(1.0, True)] * 10 + [(0.0, False)] * 10
    out = _summarise(rows)
    assert out["ece"] == 0.0
    assert out["brier"] == 0.0
    assert out["base_rate"] == 0.5


def test_confident_and_wrong_is_the_worst_score():
    """Certain every time, wrong every time: maximum error on both measures."""
    out = _summarise([(1.0, False)] * 8)
    assert out["ece"] == 1.0
    assert out["brier"] == 1.0
    assert out["base_rate"] == 0.0


def test_ece_is_the_gap_between_stated_confidence_and_hit_rate():
    """Eight calls at 80% confidence, half right. The claim was 0.8, the truth 0.5, so the gap is
    0.3 — and with every call in one bucket the weighted mean gap is just that."""
    rows = [(0.8, True)] * 4 + [(0.8, False)] * 4
    out = _summarise(rows)
    assert out["ece"] == pytest.approx(0.3)
    assert out["brier"] == pytest.approx(0.5 * (0.2**2) + 0.5 * (0.8**2))


def test_ece_weights_buckets_by_how_many_calls_they_hold():
    """A bucket holding one call must not move the score as much as one holding nine — otherwise a
    single stray forecast at an unusual confidence would dominate the whole diagram."""
    rows = [(0.9, True)] * 9 + [(0.1, True)]      # the 0.1 call is wildly under-confident
    out = _summarise(rows)
    # 0.9 bucket: |0.9 - 1.0| = 0.1, weight 9/10. 0.1 bucket: |0.1 - 1.0| = 0.9, weight 1/10.
    assert out["ece"] == pytest.approx(0.9 * 0.1 + 0.1 * 0.9)


def test_full_confidence_lands_in_the_top_bucket():
    """A confidence of exactly 1.0 must not fall off the end of the ten buckets."""
    assert _bin_index(1.0) == 9
    assert _bin_index(0.0) == 0
    assert _bin_index(0.55) == 5


def test_a_thin_sample_is_reported_but_flagged_uncalibratable():
    """Hiding the numbers until the floor is reached would leave an operator unable to see whether
    the sample is even growing. Reporting them as authoritative would be worse."""
    out = _summarise([(0.7, True)] * 5)
    assert out["n_decisions"] == 5
    assert out["is_calibratable"] is False
    assert out["ece"] is not None, "the statistics are still computed and shown"

    plenty = _summarise([(0.7, True)] * MIN_N_FOR_CALIBRATION)
    assert plenty["is_calibratable"] is True


def test_no_scored_calls_yields_an_empty_grid_not_a_crash():
    """The reliability diagram should draw its axes on an empty dataset rather than collapse."""
    out = _summarise([])
    assert out["n_decisions"] == 0
    assert out["ece"] is None and out["brier"] is None
    assert len(out["bins"]) == 10
    assert all(b["n"] == 0 for b in out["bins"])
