"""The model wrappers and the orchestrator, and what they do when they cannot answer.

The heavy dependencies (xgboost, scikit-learn, statsmodels) are deliberately NOT installed here —
they live in the separate Testing Lab image. That is exactly the condition these tests exist for:
every wrapper must import, construct and report its manifest in an environment where its library is
absent, and must REFUSE to produce a number rather than inventing one.

The refusals below replaced placeholder returns during the port. `predict()` on an untrained model
returned 0.5 in the original; ARIMA returned 0.5 in three separate failure paths; the orchestrator
substituted 0.5 for any model it could not run. 0.5 is not "unknown", it is a confident coin flip
that is then averaged into a composite and, worse, SHRINKS the panel's standard deviation — so the
tickers with the least information looked like the tickers with the most model agreement.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.ml.arima_model import ARIMAModel
from src.ml.elastic_net_model import ElasticNetDirectionModel
from src.ml.errors import UntrainedModelError
from src.ml.orchestrator import (
    HIGH_MODEL_DISAGREEMENT_THRESHOLD,
    QuantModelOrchestrator,
)
from src.ml.random_forest_model import RandomForestDirectionModel
from src.ml.xgboost_model import XGBoostDirectionModel

FUNDAMENTALS = {"rsi": 55.0, "macd": 0.2, "volume_ratio": 1.1}

DIRECTION_MODELS = [
    XGBoostDirectionModel,
    RandomForestDirectionModel,
    ElasticNetDirectionModel,
]


# -- the wrappers ------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", DIRECTION_MODELS)
def test_untrained_predict_raises_instead_of_returning_a_placeholder(cls):
    """Break: return 0.5 from an untrained predict(). This is what catches it."""
    with pytest.raises(UntrainedModelError):
        cls().predict(FUNDAMENTALS)


@pytest.mark.parametrize("cls", [*DIRECTION_MODELS, ARIMAModel])
def test_constructs_and_reports_untrained_without_its_heavy_dependency(cls):
    """The wrapper is importable and honest about its state with no library installed."""
    manifest = cls().get_manifest()
    assert manifest["trained"] is False
    # One manifest shape across the panel: a sibling that named its fields differently would drop
    # out of every leaderboard and run record that reads them by key.
    assert manifest["model_name"] == cls.__name__
    assert manifest["version"]
    assert manifest["output_range"] == [0.0, 1.0]
    assert "parameters" in manifest


def test_arima_refuses_when_statsmodels_is_absent():
    """Missing library is a refusal, not a 0.5. One of ARIMA's three original placeholder paths."""
    try:
        import statsmodels  # noqa: F401
    except ImportError:
        with pytest.raises(UntrainedModelError, match="statsmodels"):
            ARIMAModel().predict(np.linspace(100, 110, 60))
    else:
        pytest.skip("statsmodels installed; the absent-library path cannot be exercised")


def test_arima_refuses_on_too_short_a_history():
    """Second original placeholder path: fewer observations than ARIMA can fit."""
    with pytest.raises(UntrainedModelError):
        ARIMAModel().predict(np.linspace(100, 105, 5))


def test_random_forest_importances_require_training():
    with pytest.raises(UntrainedModelError):
        RandomForestDirectionModel().feature_importances()


def test_random_forest_matches_the_sibling_interface():
    """Joe's ask: a RandomForest with the same interface as the ported models."""
    reference = ElasticNetDirectionModel()
    rf = RandomForestDirectionModel()
    for method in ("train", "predict", "save", "load", "get_manifest"):
        assert callable(getattr(rf, method)), method
        assert callable(getattr(reference, method)), method


def test_random_forest_is_depth_capped():
    """An unbounded forest memorises a short daily series and validates beautifully on nothing."""
    params = RandomForestDirectionModel().get_manifest()["parameters"]
    assert params["max_depth"] is not None and params["max_depth"] <= 10
    assert params["min_samples_leaf"] >= 5


# -- the orchestrator --------------------------------------------------------------------------


def test_a_model_that_cannot_run_abstains_and_is_named():
    """No fundamentals, no history: nothing scores, and the result says so rather than showing 0.5."""
    result = QuantModelOrchestrator().score_ticker("AAPL")

    assert result["scores"] == {}
    assert result["models_scored"] == 0
    assert set(result["abstentions"]) == {"xgboost", "random_forest", "elastic_net", "arima"}
    assert result["abstentions"]["arima"] == "no OHLCV history supplied"


def test_unmeasured_composite_is_none_not_a_neutral_number():
    """Break: fall back to 0.5 for the composite. `None` is the only honest answer here."""
    result = QuantModelOrchestrator().score_ticker("AAPL")

    assert result["composite"] is None
    assert result["std_dev"] is None
    # None, not False — False would read as "the panel agreed" when the panel never convened.
    assert result["high_disagreement_flag"] is None


def test_arima_abstention_names_the_observation_shortfall():
    result = QuantModelOrchestrator().score_ticker("AAPL", ohlcv_df=np.linspace(100, 105, 12))
    assert "12 observations" in result["abstentions"]["arima"]


def test_untrained_models_abstain_when_fundamentals_are_supplied():
    """Fundamentals present, models unfitted: still an abstention, never a fabricated score."""
    result = QuantModelOrchestrator().score_ticker("AAPL", fundamentals=FUNDAMENTALS)

    assert result["scores"] == {}
    for name in ("xgboost", "random_forest", "elastic_net"):
        assert "before train()" in result["abstentions"][name]


def _stub(orch, **scores):
    """Replace the panel's members with models that answer, or fail, on command."""

    class _Fixed:
        def __init__(self, value):
            self._value = value

        def predict(self, _features):
            if isinstance(self._value, Exception):
                raise self._value
            return self._value

    for name, value in scores.items():
        setattr(orch, f"_{name}", _Fixed(value))
    return orch


def test_composite_and_dispersion_cover_only_the_models_that_scored():
    """One abstainer must not drag the mean toward neutral or compress the spread."""
    orch = _stub(
        QuantModelOrchestrator(),
        xgboost=0.9,
        random_forest=0.8,
        elastic_net=UntrainedModelError("ElasticNet.predict() called before train()."),
    )
    result = orch.score_ticker("AAPL", fundamentals=FUNDAMENTALS)

    assert result["scores"] == {"xgboost": 0.9, "random_forest": 0.8}
    assert result["models_scored"] == 2
    assert result["composite"] == pytest.approx(0.85)
    # Averaging in a 0.5 for the abstainer would give 0.7333 and a much tighter spread.
    assert result["composite"] != pytest.approx(0.7333, abs=1e-3)
    assert "elastic_net" in result["abstentions"]


def test_high_disagreement_is_flagged_against_the_inlined_threshold():
    orch = _stub(QuantModelOrchestrator(), xgboost=0.99, random_forest=0.01)
    result = orch.score_ticker("AAPL", fundamentals=FUNDAMENTALS)

    assert result["std_dev"] > HIGH_MODEL_DISAGREEMENT_THRESHOLD
    assert result["high_disagreement_flag"] is True


def test_agreeing_models_are_not_flagged():
    orch = _stub(QuantModelOrchestrator(), xgboost=0.71, random_forest=0.69)
    assert orch.score_ticker("AAPL", fundamentals=FUNDAMENTALS)["high_disagreement_flag"] is False


def test_an_unexpected_failure_abstains_loudly_rather_than_silently():
    """An abstention must never become a quiet way to lose a real error."""
    orch = _stub(
        QuantModelOrchestrator(),
        xgboost=0.6,
        random_forest=ValueError("feature shape mismatch"),
    )
    result = orch.score_ticker("AAPL", fundamentals=FUNDAMENTALS)

    assert result["abstentions"]["random_forest"] == "prediction failed: ValueError"
    assert result["scores"] == {"xgboost": 0.6}


def test_one_tickers_failure_does_not_contaminate_another():
    orch = _stub(QuantModelOrchestrator(), xgboost=0.6, random_forest=0.4)
    results = orch.score_multiple(
        ["AAPL", "MSFT"], fundamentals_data={"AAPL": FUNDAMENTALS}
    )

    assert results["AAPL"]["models_scored"] == 2
    assert results["MSFT"]["models_scored"] == 0
    assert results["MSFT"]["composite"] is None


def test_agreement_metrics_separate_unscored_tickers_from_agreeing_ones():
    """A run that mostly abstained must not read as a run that mostly agreed."""
    orch = _stub(QuantModelOrchestrator(), xgboost=0.95, random_forest=0.05)
    metrics = orch.get_agreement_metrics(
        ["AAPL", "MSFT", "NVDA"], fundamentals_data={"AAPL": FUNDAMENTALS}
    )

    assert metrics["tickers_requested"] == 3
    assert metrics["tickers_scored"] == 1
    assert metrics["tickers_unscored"] == 2
    assert metrics["high_disagreement_count"] == 1
    assert metrics["high_disagreement_tickers"] == ["AAPL"]


def test_agreement_metrics_report_none_when_nothing_scored():
    metrics = QuantModelOrchestrator().get_agreement_metrics(["AAPL", "MSFT"])

    assert metrics["tickers_scored"] == 0
    assert metrics["avg_std_dev"] is None
    assert metrics["max_std_dev"] is None


def test_every_panel_member_appears_in_the_manifests():
    manifests = QuantModelOrchestrator().get_all_manifests()
    assert set(manifests) == {"xgboost", "random_forest", "elastic_net", "arima"}
    assert all(m["trained"] is False for m in manifests.values())


# -- the leaderboard ---------------------------------------------------------------------------


def test_unmeasured_models_are_excluded_from_the_disagreement_spread():
    """validation.py marks a run that measured nothing; the leaderboard must not average it in."""
    from src.ml.model_comparison import ModelComparison

    comparison = ModelComparison()
    comparison.add_result("xgboost", {"accuracy": 0.61, "sharpe_ratio": 1.2, "measured": True})
    comparison.add_result("elastic_net", {"accuracy": 0.58, "sharpe_ratio": 0.9, "measured": True})
    comparison.add_result("arima", {"accuracy": 0.0, "sharpe_ratio": 0.0, "measured": False})

    disagreement = comparison.disagreement_analysis()

    assert disagreement["unmeasured_models"] == ["arima"]
    assert set(disagreement["models"]) == {"xgboost", "elastic_net"}
    # The old 0.5 default would have stretched the range from 0.03 to 0.11.
    assert disagreement["accuracy_spread"]["range"] == pytest.approx(0.03)


def test_disagreement_needs_two_measured_models_not_two_rows():
    from src.ml.model_comparison import ModelComparison

    comparison = ModelComparison()
    comparison.add_result("xgboost", {"accuracy": 0.61, "measured": True})
    comparison.add_result("arima", {"accuracy": 0.0, "measured": False})

    disagreement = comparison.disagreement_analysis()

    assert "error" in disagreement
    assert disagreement["unmeasured_models"] == ["arima"]
