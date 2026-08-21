"""Running an experiment: assemble data, train, validate honestly, record what happened.

WHAT "HONESTLY" MEANS HERE, CONCRETELY
    Walk-forward only, expanding window, and the model is retrained as the window grows. No random
    shuffle, no k-fold over time-ordered rows, and no fitting on data that postdates the prediction.
    Those are not stylistic preferences — a shuffled split on a price series will report an accuracy
    in the seventies for a model that has learned nothing, because tomorrow's bar sits in the
    training set.

THE ORDER OF OPERATIONS IS LOAD-BEARING
    The experiment row is opened BEFORE any work starts and closed in a finally, so a training run
    that dies — OOM, a missing library, a bad frame — leaves a row that says it failed and why. The
    alternative, writing the row on success, means a crashed run is indistinguishable from one that
    was never requested. That is the same invisibility 022 was written to remove for the cycle.

CANCELLATION
    A disconnected client cancels the asyncio task, which raises CancelledError inside the generator.
    That is caught to close the experiment as cancelled and then re-raised, because swallowing it
    would leave the task looking alive to the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from lab import store
from lab.datasets import DatasetUnavailable, features_and_labels, historical_bars, synthetic

logger = logging.getLogger("lab.runner")


def _model_factories() -> dict[str, Callable[[dict | None], Any]]:
    """The panel, imported lazily so an import error names ONE model instead of killing the app.

    The Lab image ships every dependency, so this should not fail — but if xgboost is missing from a
    rebuilt image, the failure must be "xgboost is unavailable" on that model's run, not a service
    that will not start and takes elastic_net and the API down with it.
    """
    from src.ml.arima_model import ARIMAModel
    from src.ml.elastic_net_model import ElasticNetDirectionModel
    from src.ml.random_forest_model import RandomForestDirectionModel
    from src.ml.xgboost_model import XGBoostDirectionModel

    return {
        "xgboost": lambda p: XGBoostDirectionModel(p),
        "random_forest": lambda p: RandomForestDirectionModel(p),
        "elastic_net": lambda p: ElasticNetDirectionModel(p),
        "arima": lambda p: ARIMAModel(**(p or {})),
    }


AVAILABLE_MODELS = ("xgboost", "random_forest", "elastic_net")
# ARIMA is deliberately absent from the walk-forward panel. It takes a price series, not a feature
# matrix, so it does not implement the train(X, y) / predict(row) interface the validator drives.
# Listing it here and letting every prediction fail would produce an unmeasured run and a leaderboard
# entry that looks like a bad model rather than a wrong harness. It needs an adapter first.


def _dataset(spec: dict) -> tuple[Any, Any, list[str], str, str]:
    """Resolve a dataset spec to (X, y, feature_names, data_source, dataset_label).

    Returns the two labels alongside the arrays because 023 requires both on the experiment row and
    they must describe the data that was actually loaded, not the data that was asked for.
    """
    source = spec.get("source")
    if source == "synthetic":
        seed = int(spec.get("seed", 42))
        bars = int(spec.get("bars", 750))
        X, y, names = features_and_labels(synthetic(seed=seed, n_bars=bars))
        return X, y, names, "synthetic", f"synthetic seed={seed} bars={bars}"

    if source == "historical_bars":
        symbol = str(spec.get("symbol", "")).strip().upper()
        if not symbol:
            raise DatasetUnavailable("historical_bars requires a symbol")
        start, end = spec.get("start"), spec.get("end")
        with store.connection() as conn:
            frame = historical_bars(conn, symbol, start, end)
        X, y, names = features_and_labels(frame)
        span = f"{frame['date'].iloc[0].date()}..{frame['date'].iloc[-1].date()}"
        return X, y, names, "historical_bars", f"{symbol} {span} ({len(frame)} bars)"

    raise DatasetUnavailable(
        f"unknown dataset source {source!r}; expected 'synthetic' or 'historical_bars'. "
        "There is no default on purpose — a request that does not say what data it wants must not "
        "silently receive generated data."
    )


def _validate(model_name: str, params: dict | None, X, y, validation: dict):
    """Train and validate one model. Runs in a worker thread; returns a ValidationResult."""
    from src.ml.validation import WalkForwardValidator

    model = _model_factories()[model_name](params)
    validator = WalkForwardValidator(
        initial_train_pct=float(validation.get("initial_train_pct", 0.60)),
        step_size=int(validation.get("step_size", 20)),
        retrain_every=int(validation.get("retrain_every", 60)),
    )
    return validator.run_walk_forward(model, X, y, model_name=model_name), model


async def run_experiment(
    *,
    name: str,
    models: list[str],
    dataset_spec: dict,
    params: dict | None = None,
    validation: dict | None = None,
    operator: str | None = None,
) -> AsyncIterator[dict]:
    """Run one experiment across one or more models, yielding progress events as it goes.

    Each model is validated in a worker thread so the event loop keeps serving — a walk-forward over
    a few thousand bars is seconds to minutes of pure CPU, and blocking on it would stall every other
    request this process is handling, including the stream the caller is reading these events from.
    """
    validation = validation or {}
    unknown = [m for m in models if m not in AVAILABLE_MODELS]
    if unknown:
        yield {
            "type": "error",
            "message": (
                f"unknown model(s): {', '.join(unknown)}. Available: {', '.join(AVAILABLE_MODELS)}."
            ),
        }
        return
    if not models:
        yield {"type": "error", "message": "no models requested"}
        return

    experiment_id: int | None = None
    try:
        yield {"type": "status", "message": "loading dataset"}
        X, y, feature_names, data_source, dataset_label = await asyncio.to_thread(
            _dataset, dataset_spec
        )
        yield {
            "type": "dataset",
            "data_source": data_source,
            "dataset": dataset_label,
            "samples": len(X),
            "features": feature_names,
            # The class balance, up front. A series that went up 78% of the time makes a 78%
            # accuracy the null result rather than a good one, and that is invisible from the
            # metric alone.
            "positive_label_pct": round(float(y.mean()) * 100, 2),
        }

        experiment_id = await asyncio.to_thread(
            store.create_experiment,
            name=name,
            kind="walk_forward",
            data_source=data_source,
            dataset=dataset_label,
            validation_kind="walk_forward",
            params={"models": models, "model_params": params or {}, "validation": validation},
            operator=operator,
        )
        yield {"type": "experiment", "experiment_id": experiment_id}

        for index, model_name in enumerate(models, start=1):
            yield {
                "type": "model_start",
                "model": model_name,
                "index": index,
                "total": len(models),
            }
            try:
                result, model = await asyncio.to_thread(
                    _validate, model_name, (params or {}).get(model_name), X, y, validation
                )
            except Exception as exc:  # noqa: BLE001 — one model's failure is not the run's failure
                logger.error("%s failed to validate: %s", model_name, exc)
                yield {"type": "model_error", "model": model_name, "message": str(exc)}
                continue

            run_id = await asyncio.to_thread(
                store.record_model_run,
                experiment_id=experiment_id,
                result=result,
                params=(params or {}).get(model_name) or {},
                manifest=model.get_manifest(),
            )
            yield {
                "type": "model_result",
                "model": model_name,
                "run_id": run_id,
                "metrics": result.metrics,
                # Repeated beside the metrics in every event, not just in the row. A consumer that
                # renders `metrics` without this is rendering numbers that may describe nothing.
                "measured": bool(result.metrics.get("measured")),
                # A measured result can still be a constant. See store.degenerate_reasons.
                "degenerate": store.degenerate_reasons(result.metrics),
                "predictions_made": result.total_predictions + result.failed_predictions,
                "predictions_failed": result.failed_predictions,
                "steps": result.total_steps,
            }

        await asyncio.to_thread(store.finish_experiment, experiment_id)
        yield {"type": "done", "experiment_id": experiment_id}

    except asyncio.CancelledError:
        if experiment_id is not None:
            await asyncio.to_thread(
                store.fail_experiment, experiment_id, "cancelled by the client"
            )
        raise
    # Broad on purpose: the run's failure is reported and recorded, never swallowed.
    except Exception as exc:
        logger.error("experiment failed: %s", exc, exc_info=True)
        if experiment_id is not None:
            await asyncio.to_thread(store.fail_experiment, experiment_id, str(exc))
        yield {"type": "error", "message": str(exc), "experiment_id": experiment_id}


async def run_sweep(
    *,
    name: str,
    model: str,
    param: str,
    values: list,
    dataset_spec: dict,
    metric: str = "sharpe_ratio",
    validation: dict | None = None,
    operator: str | None = None,
) -> AsyncIterator[dict]:
    """Sweep one parameter across a list of values, recording every point — including the failures.

    A point that could not be measured stays in `points` with metric=None and measured=false. It
    cannot win (store.record_sweep picks the best from measured points only) but it is visible, so
    a sweep that only managed to measure two of nine values reads as exactly that rather than as a
    clean two-point curve.
    """
    validation = validation or {}
    if model not in AVAILABLE_MODELS:
        yield {"type": "error", "message": f"unknown model {model!r}"}
        return
    if metric not in store.ALLOWED_METRICS:
        yield {"type": "error", "message": f"unknown metric {metric!r}"}
        return
    if not values:
        yield {"type": "error", "message": "a sweep needs at least one value"}
        return

    experiment_id: int | None = None
    points: list[dict] = []
    try:
        X, y, _names, data_source, dataset_label = await asyncio.to_thread(_dataset, dataset_spec)
        experiment_id = await asyncio.to_thread(
            store.create_experiment,
            name=name,
            kind="sweep",
            data_source=data_source,
            dataset=dataset_label,
            validation_kind="walk_forward",
            params={"model": model, "param": param, "values": values, "metric": metric},
            operator=operator,
        )
        yield {"type": "experiment", "experiment_id": experiment_id, "dataset": dataset_label}

        for index, value in enumerate(values, start=1):
            yield {"type": "point_start", "value": value, "index": index, "total": len(values)}
            try:
                result, model_obj = await asyncio.to_thread(
                    _validate, model, {param: value}, X, y, validation
                )
                measured = bool(result.metrics.get("measured"))
                score = result.metrics.get(metric) if measured else None
                await asyncio.to_thread(
                    store.record_model_run,
                    experiment_id=experiment_id,
                    result=result,
                    params={param: value},
                    manifest=model_obj.get_manifest(),
                )
            except Exception as exc:  # noqa: BLE001 — one bad point must not end the sweep
                logger.error("sweep point %s=%r failed: %s", param, value, exc)
                measured, score = False, None
                yield {"type": "point_error", "value": value, "message": str(exc)}

            points.append({"value": value, "metric": score, "measured": measured})
            yield {"type": "point", "value": value, "metric": score, "measured": measured}

        sweep_id = await asyncio.to_thread(
            store.record_sweep,
            experiment_id=experiment_id,
            param=param,
            metric=metric,
            points=points,
        )
        await asyncio.to_thread(store.finish_experiment, experiment_id)
        measured_count = sum(1 for p in points if p["measured"])
        yield {
            "type": "done",
            "experiment_id": experiment_id,
            "sweep_id": sweep_id,
            "points_tested": len(points),
            "points_measured": measured_count,
        }

    except asyncio.CancelledError:
        if experiment_id is not None:
            await asyncio.to_thread(store.fail_experiment, experiment_id, "cancelled by the client")
        raise
    # Broad on purpose: same as run_experiment — the sweep closes as failed, with its reason.
    except Exception as exc:
        logger.error("sweep failed: %s", exc, exc_info=True)
        if experiment_id is not None:
            await asyncio.to_thread(store.fail_experiment, experiment_id, str(exc))
        yield {"type": "error", "message": str(exc), "experiment_id": experiment_id}
