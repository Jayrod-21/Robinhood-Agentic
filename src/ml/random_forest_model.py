"""Random Forest direction model — the sibling Joe asked for, mirroring the others.

NOT PORTED — WRITTEN
    The Special-Sprinkle-Sauce repo has XGBoost, ElasticNet and ARIMA. Random Forest was requested
    and does not exist there, so this follows the same train / predict / save / load / get_manifest
    interface the validator and the comparison leaderboard already call, and lazy-imports sklearn
    exactly as its siblings do so importing this package costs nothing in an environment without it.

WHY IT EARNS ITS PLACE BESIDE XGBOOST
    Both are tree ensembles and will often agree, which is the point: ModelComparison's disagreement
    analysis is only informative when the models can genuinely differ. A bagged forest and a boosted
    ensemble fail differently — the forest is far harder to overfit on a short, noisy series, and a
    disagreement between them is a signal about the data rather than about a hyperparameter.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from .errors import UntrainedModelError

logger = logging.getLogger("agentic.ml.random_forest")

# Depth is capped and leaves are floored on purpose. Daily bars over a few years are a short, noisy
# series, and an unbounded forest will memorise it — producing a validation score that looks superb
# and forecasts nothing.
DEFAULT_PARAMS = {
    "n_estimators": 300,
    "max_depth": 6,
    "min_samples_leaf": 20,
    "random_state": 42,
    "n_jobs": -1,
}


class RandomForestDirectionModel:
    """Bagged tree ensemble for 5-day forward direction. Outputs P(up) in [0, 1]."""

    def __init__(self, params: dict | None = None):
        self._params = params or DEFAULT_PARAMS.copy()
        self._model = None
        self._version = "1.0.0"
        self._trained = False

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray | None = None,
        y_val: np.ndarray | None = None,
    ) -> dict:
        """Fit the forest. Returns training/validation metrics."""
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError:
            logger.error("scikit-learn not installed")
            return {"error": "scikit-learn not installed"}

        self._model = RandomForestClassifier(**self._params)
        self._model.fit(X_train, np.asarray(y_train).astype(int))
        self._trained = True

        metrics = {"train_samples": len(X_train)}
        if X_val is not None and y_val is not None:
            preds = (self._model.predict_proba(X_val)[:, 1] > 0.5).astype(int)
            accuracy = float(np.mean(preds == np.asarray(y_val).astype(int)))
            metrics["val_samples"] = len(X_val)
            metrics["val_accuracy"] = round(accuracy, 4)
            logger.info(f"RandomForest trained — val accuracy: {accuracy:.4f}")
        return metrics

    def predict(self, features: dict | np.ndarray) -> float:
        """P(up) for a single sample.

        Raises UntrainedModelError when called before train(). A placeholder here would be averaged
        into the metrics as if it were a forecast — see src/ml/errors.py.
        """
        if self._model is None:
            raise UntrainedModelError(
                "RandomForestDirectionModel.predict() called before train(). Returning a "
                "placeholder here would be averaged into the metrics as if it were a real forecast."
            )

        if isinstance(features, dict):
            X = np.array([list(features.values())])
        else:
            X = features.reshape(1, -1) if features.ndim == 1 else features

        return float(self._model.predict_proba(X)[0][1])

    def feature_importances(self, names: list[str] | None = None) -> dict[str, float]:
        """What the forest actually leaned on.

        The reason to run a forest beside a boosted ensemble: importances are cheap here and are the
        most direct answer to "is this model reading the signal I think it is, or one artefact?"
        """
        if self._model is None:
            raise UntrainedModelError("feature_importances() called before train()")
        values = [float(v) for v in self._model.feature_importances_]
        keys = names or [f"f{i}" for i in range(len(values))]
        return dict(zip(keys, values, strict=True))

    def save(self, path: str | Path) -> None:
        """Persist via joblib, with a manifest beside it."""
        if self._model is None:
            logger.warning("No model to save")
            return
        try:
            import joblib

            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(self._model, path)
            path.with_suffix(".manifest.json").write_text(
                json.dumps(self.get_manifest(), indent=2), encoding="utf-8"
            )
            logger.info(f"RandomForest model saved to {path}")
        except Exception as e:  # noqa: BLE001 — a failed save must not lose the fitted model
            logger.error(f"Failed to save RandomForest model: {e}")

    def load(self, path: str | Path) -> None:
        """Load a persisted forest. On failure the model stays untrained, and predict() says so."""
        try:
            import joblib

            self._model = joblib.load(path)
            self._trained = True
            logger.info(f"RandomForest model loaded from {path}")
        except FileNotFoundError:
            logger.error(f"Model file not found: {path}")
        except Exception as e:  # noqa: BLE001 — an unreadable artifact leaves the model untrained,
            # which predict() reports as UntrainedModelError rather than a placeholder forecast
            logger.error(f"Failed to load RandomForest model: {e}")

    def get_manifest(self) -> dict:
        """Manifest in the same shape the ported siblings emit — same keys, same order.

        The leaderboard and the run records read these by key, so a sibling that named its fields
        differently would silently drop out of every comparison it appeared in.
        """
        return {
            "model_name": "RandomForestDirectionModel",
            "version": self._version,
            "model_type": "classification",
            "target": "5-day forward return direction",
            "output_range": [0.0, 1.0],
            "parameters": self._params,
            "trained": self._trained,
            "survivorship_bias_audited": False,
        }
