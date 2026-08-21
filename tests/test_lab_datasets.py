"""The Lab's dataset layer: the label bug it routes around, and the fallback it refuses to make.

Two properties, both of which fail silently if they regress — which is why they are pinned here
rather than left to be noticed in a metric that looks slightly off.

    1. Features and labels are aligned on date, and rows whose forward return is not yet knowable
       are DROPPED. FeatureEngineer.build_labels turns that unknown into a 0, labelling the most
       recent week of every series "down" regardless of what happened.
    2. A request for real data that cannot be served RAISES. It never returns synthetic data. The
       repo this was ported from does exactly that in two places, by Joe's own account of it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.datasets import (
    MIN_BARS,
    DatasetUnavailable,
    features_and_labels,
    historical_bars,
    synthetic,
)


def _rising(n: int = 400) -> pd.DataFrame:
    """A rising series with a wobble: every 5-day forward return is positive, so every honest label
    is 1. Any 0 in the output is a fabricated label, which makes this frame a detector.

    The wobble is not decoration. A perfectly monotonic series has no down days at all, so RSI-14's
    average loss is zero for every row, rs is NaN, and build_features drops the entire frame — the
    detector would never get as far as producing a label. The amplitude is chosen so daily deltas
    change sign (RSI is defined) while the 5-day trend never does (every true label stays 1).
    """
    trend = np.linspace(100.0, 200.0, n)
    close = trend + 0.4 * np.sin(np.arange(n))
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2021-01-01", periods=n),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 1_000_000),
        }
    )


# ── the label bug ─────────────────────────────────────────────────────────────────────────────


def test_the_ported_label_builder_fabricates_the_final_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a test of our code — a test of the thing our code exists to avoid.

    If FeatureEngineer.build_labels is ever fixed upstream this test fails, which is the correct
    signal: the workaround in features_and_labels can then be reconsidered.
    """
    from src.ml.feature_engineer import FeatureEngineer

    labels = FeatureEngineer.build_labels(_rising(), forward_days=5)

    assert list(labels[:-5].unique()) == [1], "a monotonically rising series is entirely up"
    # The last five have no forward return. NaN > 0 is False, and astype(int) makes that a 0.
    assert list(labels[-5:]) == [0, 0, 0, 0, 0]


def test_unknowable_labels_are_dropped_not_guessed() -> None:
    """Break: label the trailing rows 0 instead of dropping them. This goes red."""
    X, y, _names = features_and_labels(_rising(), forward_days=5)

    assert set(np.unique(y)) == {1}, (
        "a monotonically rising series has no down days; a 0 here is a fabricated label leaking in "
        "from the last rows, whose forward return is not knowable yet"
    )
    assert len(X) == len(y)


def test_features_and_labels_are_aligned_on_date_not_zipped() -> None:
    """build_features drops the leading rows its rolling windows consume, so its output is a subset
    of the input. Zipping it against labels from the full frame shifts every label ~50 rows — a
    model that trains beautifully on noise."""
    from src.ml.feature_engineer import FeatureEngineer

    bars = _rising()
    features = FeatureEngineer.build_features(bars)
    _X, y, _names = features_and_labels(bars, forward_days=5)

    assert len(features) < len(bars), "the rolling windows must consume some leading rows"
    # Aligned length: the features that survived, minus the trailing rows with no knowable label.
    assert len(y) == len(features) - 5


def test_every_feature_column_is_returned_by_name() -> None:
    X, _y, names = features_and_labels(synthetic(seed=7))
    assert X.shape[1] == len(names)
    assert "date" not in names, "the date is an index, not a feature — it must never be trained on"
    assert "rsi_14" in names


def test_a_series_too_short_to_survive_its_own_windows_refuses() -> None:
    with pytest.raises(DatasetUnavailable):
        features_and_labels(_rising(n=40))


# ── synthetic data, honest about being synthetic ──────────────────────────────────────────────


def test_synthetic_bars_obey_the_same_ohlc_invariant_as_real_ones() -> None:
    """price_bars_daily CHECKs open and close inside [low, high]. Generated frames that violate it
    would train models on bars the database would have rejected."""
    bars = synthetic(seed=3, n_bars=300)

    assert (bars["high"] >= bars["low"]).all()
    assert bars["open"].between(bars["low"], bars["high"]).all()
    assert bars["close"].between(bars["low"], bars["high"]).all()
    assert (bars["volume"] > 0).all()


def test_synthetic_is_reproducible_from_its_seed() -> None:
    """The seed is what `dataset` records on the experiment row. If it does not reproduce the data,
    that column names a run nobody can repeat."""
    assert synthetic(seed=11).equals(synthetic(seed=11))
    assert not synthetic(seed=11)["close"].equals(synthetic(seed=12)["close"])


# ── the refusal ───────────────────────────────────────────────────────────────────────────────


class _EmptyDb:
    """A database with no bars for anything."""

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return []


class _ThinDb:
    """A database holding fewer bars than the rolling windows and a walk-forward split need."""

    def __init__(self, n: int):
        self._n = n

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return [(pd.Timestamp("2024-01-01"), 1.0, 1.0, 1.0, 1.0, 1)] * self._n


def test_a_symbol_with_no_bars_raises_rather_than_returning_synthetic_data() -> None:
    """The single most important behaviour in this module. A Lab that answers a request for real
    data with generated data produces a leaderboard that is worse than empty — it looks full."""
    with pytest.raises(DatasetUnavailable, match="Refusing rather than substituting"):
        historical_bars(_EmptyDb(), "NOSUCH")


def test_a_symbol_with_too_little_history_raises_and_says_how_much_it_had() -> None:
    with pytest.raises(DatasetUnavailable) as exc:
        historical_bars(_ThinDb(MIN_BARS - 1), "THIN")

    assert str(MIN_BARS - 1) in str(exc.value)
    assert str(MIN_BARS) in str(exc.value)
