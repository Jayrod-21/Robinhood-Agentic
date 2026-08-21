"""The Lab runner's control flow: what it records when things go wrong.

Every test here runs with xgboost, scikit-learn and statsmodels absent — the models and the store
are stubbed. That is not a compromise: what is under test is bookkeeping, not arithmetic. Does a
crashed run leave a row that says it failed and why? Does one bad model end the whole experiment?
Can a sweep point that measured nothing win the sweep? None of that needs a real gradient booster,
and requiring one would mean these never run in CI, where the heavy stack is deliberately absent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import numpy as np
import pytest

from lab import runner
from lab import store as _real_store


@dataclass
class _Result:
    """Stands in for validation.ValidationResult."""

    model_name: str
    total_steps: int = 4
    total_predictions: int = 100
    failed_predictions: int = 0
    metrics: dict = field(default_factory=lambda: {"measured": True, "sharpe_ratio": 1.2})


class _FakeStore:
    """Records what the runner would have written, and can be told to be unavailable.

    Stubs the PERSISTENCE only. `degenerate_reasons` is pure metric arithmetic with no database in
    it, so it delegates to the real implementation — reimplementing it here would let the runner's
    events and the leaderboard's rows disagree about what "degenerate" means, and the fake would
    happily keep passing.
    """

    ALLOWED_METRICS = frozenset({"sharpe_ratio", "accuracy"})
    degenerate_reasons = staticmethod(_real_store.degenerate_reasons)

    def __init__(self) -> None:
        self.experiments: dict[int, dict] = {}
        self.runs: list[dict] = []
        self.sweeps: list[dict] = []
        self._next = 1

    def create_experiment(self, **kw):
        experiment_id = self._next
        self._next += 1
        self.experiments[experiment_id] = {**kw, "status": "running", "error": None}
        return experiment_id

    def finish_experiment(self, experiment_id):
        self.experiments[experiment_id]["status"] = "complete"

    def fail_experiment(self, experiment_id, error):
        self.experiments[experiment_id].update(status="failed", error=error)

    def record_model_run(self, *, experiment_id, result, params=None, manifest=None, **_kw):
        self.runs.append({"experiment_id": experiment_id, "result": result, "params": params})
        return len(self.runs)

    def record_sweep(self, *, experiment_id, param, metric, points):
        measured = [p for p in points if p["measured"] and p["metric"] is not None]
        best = max(measured, key=lambda p: p["metric"], default=None)
        self.sweeps.append(
            {
                "experiment_id": experiment_id,
                "param": param,
                "metric": metric,
                "points": points,
                "best_value": best["value"] if best else None,
                "points_measured": len(measured),
            }
        )
        return len(self.sweeps)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(runner, "store", fake)
    return fake


@pytest.fixture
def dataset(monkeypatch: pytest.MonkeyPatch):
    """A resolved dataset, so no test here depends on pandas rolling windows."""
    rng = np.random.default_rng(0)
    X, y = rng.normal(size=(200, 4)), rng.integers(0, 2, 200)
    monkeypatch.setattr(
        runner, "_dataset", lambda _spec: (X, y, ["a", "b", "c", "d"], "synthetic", "seed=0")
    )


class _Model:
    def get_manifest(self) -> dict:
        return {"model_name": "stub", "trained": True}


def _validated(scores: dict):
    """Install a _validate that returns a canned result (or raises) per model name."""

    def _fn(model_name, _params, _X, _y, _validation):
        outcome = scores[model_name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, _Model()

    return _fn


async def _collect(agen) -> list[dict]:
    return [event async for event in agen]


def _run(**kw) -> list[dict]:
    return asyncio.run(_collect(runner.run_experiment(**kw)))


BASE = {"name": "t", "dataset_spec": {"source": "synthetic"}}


# ── refusals that happen before anything is written ───────────────────────────────────────────


def test_an_unknown_model_is_refused_without_opening_an_experiment(store, dataset) -> None:
    """A row for a run that never started is worse than no row: it shows up in the list as an
    experiment nobody can explain."""
    events = _run(models=["not_a_model"], **BASE)

    assert events[0]["type"] == "error"
    assert "not_a_model" in events[0]["message"]
    assert store.experiments == {}


def test_an_empty_model_list_is_refused(store, dataset) -> None:
    assert _run(models=[], **BASE)[0]["type"] == "error"


def test_a_dataset_that_cannot_be_loaded_fails_before_the_experiment_row(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lab.datasets import DatasetUnavailable

    def _boom(_spec):
        raise DatasetUnavailable("no bars for NOSUCH")

    monkeypatch.setattr(runner, "_dataset", _boom)
    events = _run(models=["xgboost"], **BASE)

    assert events[-1]["type"] == "error"
    assert "NOSUCH" in events[-1]["message"]
    assert store.experiments == {}


# ── what gets recorded when things go wrong mid-run ───────────────────────────────────────────


def test_one_model_failing_does_not_end_the_experiment(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break: let the exception propagate. The other two models are then never measured, and the
    experiment closes as failed over a problem with one of them."""
    monkeypatch.setattr(
        runner,
        "_validate",
        _validated(
            {
                "xgboost": _Result("xgboost"),
                "random_forest": RuntimeError("libgomp not found"),
                "elastic_net": _Result("elastic_net"),
            }
        ),
    )
    events = _run(models=["xgboost", "random_forest", "elastic_net"], **BASE)

    kinds = [e["type"] for e in events]
    assert kinds.count("model_result") == 2
    assert "model_error" in kinds
    assert kinds[-1] == "done"
    assert store.experiments[1]["status"] == "complete"
    assert len(store.runs) == 2, "a model that failed to validate must not leave a run row"


def test_a_failure_after_the_experiment_opens_closes_it_with_its_reason(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break: write the experiment row on success only. A crashed run then looks identical to one
    that was never requested — the invisibility 022 exists to prevent."""

    def _explode(**_kw):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(runner, "_validate", _validated({"xgboost": _Result("xgboost")}))
    monkeypatch.setattr(store, "record_model_run", _explode)
    events = _run(models=["xgboost"], **BASE)

    assert events[-1]["type"] == "error"
    assert store.experiments[1]["status"] == "failed"
    assert "the database went away" in store.experiments[1]["error"]


def test_a_cancelled_run_is_closed_as_failed_and_the_cancellation_is_re_raised(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swallowing CancelledError leaves the task looking alive to the event loop, and the experiment
    stuck in `running` forever."""

    def _cancel(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_validate", _cancel)

    async def _drive():
        agen = runner.run_experiment(models=["xgboost"], **BASE)
        with pytest.raises(asyncio.CancelledError):
            async for _event in agen:
                pass

    asyncio.run(_drive())
    assert store.experiments[1]["status"] == "failed"
    assert "cancelled" in store.experiments[1]["error"]


# ── what a successful run reports ─────────────────────────────────────────────────────────────


def test_the_dataset_event_names_its_source_and_its_class_balance(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A series that went up 78% of the time makes 78% accuracy the NULL result, not a good one —
    and that is invisible from the metric alone."""
    monkeypatch.setattr(runner, "_validate", _validated({"xgboost": _Result("xgboost")}))
    events = _run(models=["xgboost"], **BASE)

    dataset_event = next(e for e in events if e["type"] == "dataset")
    assert dataset_event["data_source"] == "synthetic"
    assert 0 <= dataset_event["positive_label_pct"] <= 100
    assert dataset_event["samples"] == 200


def test_every_result_event_carries_measured_beside_its_metrics(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer rendering `metrics` without this is rendering numbers that may describe nothing."""
    monkeypatch.setattr(
        runner,
        "_validate",
        _validated(
            {
                "xgboost": _Result(
                    "xgboost",
                    total_predictions=0,
                    failed_predictions=180,
                    metrics={"measured": False, "sharpe_ratio": 0.0},
                )
            }
        ),
    )
    result = next(e for e in _run(models=["xgboost"], **BASE) if e["type"] == "model_result")

    assert result["measured"] is False
    assert result["predictions_made"] == 180
    assert result["predictions_failed"] == 180


def test_the_experiment_records_the_data_source_the_loader_actually_returned(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the one the request asked for. They can differ, and the row must describe what was
    loaded."""
    monkeypatch.setattr(runner, "_validate", _validated({"xgboost": _Result("xgboost")}))
    _run(models=["xgboost"], **BASE)

    assert store.experiments[1]["data_source"] == "synthetic"
    assert store.experiments[1]["validation_kind"] == "walk_forward"


def test_arima_is_not_offered_as_a_walk_forward_model() -> None:
    """It takes a price series, not a feature matrix, so it cannot implement the validator's
    interface. Offering it would produce an unmeasured run that reads as a bad model rather than a
    wrong harness."""
    assert "arima" not in runner.AVAILABLE_MODELS
    assert "arima" in runner._model_factories()


# ── sweeps ────────────────────────────────────────────────────────────────────────────────────


def _sweep(**kw) -> list[dict]:
    return asyncio.run(
        _collect(
            runner.run_sweep(
                name="s",
                model="xgboost",
                param="max_depth",
                dataset_spec={"source": "synthetic"},
                **kw,
            )
        )
    )


def test_a_point_that_could_not_be_measured_stays_visible_and_cannot_win(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break: drop failed points from `points`. A sweep that measured two of four values then reads
    as a clean two-point curve."""
    outcomes = iter(
        [
            (_Result("xgboost", metrics={"measured": True, "sharpe_ratio": 0.4}), _Model()),
            RuntimeError("out of memory"),
            (_Result("xgboost", metrics={"measured": True, "sharpe_ratio": 1.9}), _Model()),
            (
                _Result(
                    "xgboost",
                    total_predictions=0,
                    failed_predictions=50,
                    metrics={"measured": False, "sharpe_ratio": 0.0},
                ),
                _Model(),
            ),
        ]
    )

    def _next(*_a, **_k):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(runner, "_validate", _next)
    events = _sweep(values=[2, 4, 6, 8])

    done = events[-1]
    assert done["type"] == "done"
    assert done["points_tested"] == 4
    assert done["points_measured"] == 2

    sweep = store.sweeps[0]
    assert [p["value"] for p in sweep["points"]] == [2, 4, 6, 8], "every value keeps its place"
    assert sweep["points"][1]["measured"] is False
    assert sweep["points"][3]["measured"] is False
    assert sweep["best_value"] == 6


def test_a_sweep_with_nothing_measurable_reports_no_winner(
    store, dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _always_fails(*_a, **_k):
        raise RuntimeError("xgboost is not installed")

    monkeypatch.setattr(runner, "_validate", _always_fails)
    events = _sweep(values=[2, 4])

    assert events[-1]["points_measured"] == 0
    assert store.sweeps[0]["best_value"] is None


def test_a_sweep_refuses_an_unknown_metric_before_opening_an_experiment(store, dataset) -> None:
    events = _sweep(values=[2], metric="vibes")

    assert events[0]["type"] == "error"
    assert store.experiments == {}


def test_a_sweep_needs_at_least_one_value(store, dataset) -> None:
    assert _sweep(values=[])[0]["type"] == "error"
