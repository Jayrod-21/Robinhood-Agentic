"""The ported validation library, and the one thing changed on the way in.

validation.py, feature_engineer.py and model_comparison.py came from the Special-Sprinkle-Sauce
repo. The logic is unchanged except for how a FAILED PREDICTION is handled, which these pin.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.ml.validation import (
    TimeSeriesCrossValidator,
    WalkForwardValidator,
    calculate_metrics,
)


class _Working:
    def train(self, X, y, *a):
        return {}

    def predict(self, features):
        return 0.9 if features[0] > 0 else 0.1


class _Broken:
    """Never fitted. The case the Lab has to tell apart from a merely bad model."""

    def train(self, X, y, *a):
        return {}

    def predict(self, features):
        raise RuntimeError("model was never fitted")


def _data(n: int = 400, seed: int = 0):
    rng = np.random.default_rng(seed)
    feats = rng.normal(size=(n, 6))
    return feats, (feats[:, 0] > 0).astype(int)


# ── the change made during the port ───────────────────────────────────────────────────────────


def test_a_model_that_never_predicts_is_not_scored_as_a_coin_flip():
    """THE reason this file exists.

    _safe_predict returned 0.5 on any failure — a real, defensible probability, which is exactly
    what made it dangerous: it flowed straight into calculate_metrics, so a model that failed EVERY
    prediction scored an accuracy near 50% and read as "no better than chance". The truth is "this
    never ran", and a Testing Lab that cannot tell those apart is not measuring anything.
    """
    feats, labels = _data()
    result = WalkForwardValidator(initial_train_pct=0.5, step_size=20).run_walk_forward(
        _Broken(), feats, labels, model_name="broken"
    )
    assert result.total_predictions == 0, "nothing was predicted"
    assert result.failed_predictions == 200, "and every attempt is counted"
    assert result.metrics.get("measured") is False, (
        "metrics computed from nothing must announce themselves as placeholders — an accuracy of "
        "0.0 otherwise reads as 'always wrong' rather than 'never ran'"
    )


def test_a_working_model_is_measured_normally():
    """Guards the test above: if failures swallowed everything, a good model would look broken."""
    feats, labels = _data()
    result = WalkForwardValidator(initial_train_pct=0.5, step_size=20).run_walk_forward(
        _Working(), feats, labels, model_name="working"
    )
    assert result.failed_predictions == 0
    assert result.metrics["measured"] is True
    assert result.metrics["accuracy"] == pytest.approx(1.0), "a separable signal is learnable"


def test_predictions_and_actuals_are_dropped_in_pairs():
    """A failed prediction removes its label too. Keeping the actual would shift every later
    comparison by one and score the model against the wrong labels — worse than not scoring it."""
    feats, labels = _data(n=200)

    class _Flaky:
        def __init__(self):
            self.n = 0

        def train(self, X, y, *a):
            return {}

        def predict(self, features):
            self.n += 1
            if self.n % 2:
                raise RuntimeError("intermittent")
            return 0.9 if features[0] > 0 else 0.1

    result = WalkForwardValidator(initial_train_pct=0.5, step_size=20).run_walk_forward(
        _Flaky(), feats, labels, model_name="flaky"
    )
    assert result.failed_predictions > 0
    assert result.metrics["measured"] is True
    # Every surviving pair is still correctly aligned, so a separable signal stays perfectly scored.
    assert result.metrics["accuracy"] == pytest.approx(1.0), "pairs went out of alignment"


# ── guarantees inherited from the source, pinned because the Lab depends on them ──────────────


def test_the_cross_validator_leaves_a_gap_between_train_and_test():
    """The leakage guard. Without the gap, a 5-day forward label overlaps the training window and
    the model is scored on information it was trained on — which produces excellent numbers and
    tells you nothing."""
    cv = TimeSeriesCrossValidator(n_splits=3, gap_days=5)
    data = np.arange(300).reshape(-1, 1)
    for train_idx, test_idx in cv.split(data):
        assert test_idx.min() - train_idx.max() > 5, "train and test are adjacent — leakage"


def test_a_negative_gap_is_refused():
    with pytest.raises(ValueError, match="gap_days"):
        TimeSeriesCrossValidator(n_splits=3, gap_days=-1)


def test_training_always_precedes_testing_in_walk_forward():
    """Expanding window: every test index must be in the future of every train index. A validator
    that scores a model on its own past is measuring memorisation."""
    cv = TimeSeriesCrossValidator(n_splits=4, gap_days=2)
    for train_idx, test_idx in cv.split(np.arange(400).reshape(-1, 1)):
        assert train_idx.max() < test_idx.min()


def test_empty_input_is_reported_as_unmeasured_not_as_zero():
    out = calculate_metrics(np.array([]), np.array([]))
    assert out["measured"] is False
    assert out["total_predictions"] == 0
