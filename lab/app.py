"""The Lab's HTTP surface.

AUTHENTICATION: NONE HERE, AND THAT IS THE DESIGN
    This app authenticates nobody. It is reachable only from the backend container, over
    `rh-internal`, with no host port and no Caddy route — so every request that arrives has already
    passed the app-wide CSRF guard and session gate in backend/app/main.py. Duplicating that here
    would mean a second implementation of session validation, a second set of grants on the auth
    tables, and two places to get it wrong.

    The whole guarantee therefore rests on the network topology, which makes the compose file part
    of the security boundary. If a host port or a Caddy route is ever added to this service, this
    module becomes an unauthenticated endpoint that trains models on demand. Anyone touching that
    file should read this paragraph first — it is repeated in the compose comment for that reason.

    What it is NOT is a trading surface. Nothing here places an order, reads a broker credential,
    or writes a production setting; the Lab measures, and applying a result to live weights stays a
    separate confirmed write through the backend's audited PUT /api/settings/{key}.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lab import runner, store

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("lab.app")

app = FastAPI(
    title="Testing Lab",
    # Same reasoning as the backend (app/main.py): a private two-operator service documents its API
    # in its code, and an unauthenticated schema endpoint is surface for nothing.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

PREFIX = "/api/testing-lab"

# A sweep is N full walk-forward validations. Twenty-odd points over a few thousand bars is minutes
# of CPU on a box that also serves the dashboard and shares a GPU with another project, so the
# ceiling is declared here rather than discovered at runtime.
#
# A module constant, NOT a field on SweepRequest: anything declared on the request model is part of
# the request, so a caller could send its own MAX_VALUES and a later refactor reading
# `body.MAX_VALUES` instead of the default would hand the cap to the client.
MAX_SWEEP_VALUES = 24


def _sse(events: AsyncIterator[dict]) -> StreamingResponse:
    """SSE framing, matching backend/app/sse.py byte for byte.

    Reimplemented rather than imported: the Lab image ships `lab/` and `src/` and deliberately not
    `backend/`, so importing it would drag the whole FastAPI backend and its dependency tree into
    this container to reuse nine lines. The framing is identical so the frontend parser is the same
    one it already uses for the scan, debate and pipeline streams.
    """

    async def body() -> AsyncIterator[str]:
        async for event in events:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── request bodies ────────────────────────────────────────────────────────────────────────────


class DatasetSpec(BaseModel):
    """Which data to train on. `source` is required — there is no default to fall through to."""

    source: str
    seed: int = 42
    bars: int = 750
    symbol: str | None = None
    start: str | None = None
    end: str | None = None


class ValidationSpec(BaseModel):
    initial_train_pct: float = Field(0.60, ge=0.1, le=0.9)
    step_size: int = Field(20, ge=1, le=250)
    retrain_every: int = Field(60, ge=1, le=1000)


class ExperimentRequest(BaseModel):
    name: str = "experiment"
    models: list[str]
    dataset: DatasetSpec
    params: dict = Field(default_factory=dict)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    operator: str | None = None


class SweepRequest(BaseModel):
    name: str = "sweep"
    model: str
    param: str
    values: list[float | int]
    dataset: DatasetSpec
    metric: str = "sharpe_ratio"
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    operator: str | None = None


# ── routes ────────────────────────────────────────────────────────────────────────────────────


@app.get(f"{PREFIX}/health")
def health() -> dict:
    """Booleans and counts only — never a DSN, never a credential."""
    return {"service": "testing-lab", **store.health()}


@app.get(f"{PREFIX}/parameters")
def parameters() -> dict:
    """Tunable parameters with bounds, so the UI never offers a value the model will reject.

    Bounds are the Lab's own, not the production guardrails in app_settings. Nothing here can move
    a live weight, and conflating the two lists would be the first step toward something that could.
    """
    return {
        "models": {
            "xgboost": [
                {"param": "max_depth", "min": 2, "max": 10, "step": 1, "default": 4},
                {"param": "n_estimators", "min": 50, "max": 1000, "step": 50, "default": 200},
                {"param": "learning_rate", "min": 0.01, "max": 0.3, "step": 0.01, "default": 0.05},
                {"param": "subsample", "min": 0.5, "max": 1.0, "step": 0.05, "default": 0.8},
            ],
            "random_forest": [
                {"param": "max_depth", "min": 2, "max": 10, "step": 1, "default": 6},
                {"param": "n_estimators", "min": 50, "max": 1000, "step": 50, "default": 300},
                {"param": "min_samples_leaf", "min": 5, "max": 100, "step": 5, "default": 20},
            ],
            "elastic_net": [
                {"param": "alpha", "min": 0.0001, "max": 1.0, "step": 0.0001, "default": 0.01},
                {"param": "l1_ratio", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.5},
            ],
        },
        "validation": [
            {"param": "initial_train_pct", "min": 0.1, "max": 0.9, "step": 0.05, "default": 0.60},
            {"param": "step_size", "min": 1, "max": 250, "step": 1, "default": 20},
            {"param": "retrain_every", "min": 1, "max": 1000, "step": 10, "default": 60},
        ],
        "metrics": sorted(store.ALLOWED_METRICS),
        "available_models": list(runner.AVAILABLE_MODELS),
    }


@app.get(f"{PREFIX}/datasets")
def datasets(q: str | None = Query(None, min_length=1, max_length=16)) -> dict:
    """What real data actually exists, so a request never has to guess at a symbol or a range.

    Two exclusions, for the same reason: an option the caller can select but the Lab will reject is
    a worse answer than no option at all.

      * fewer bars than the rolling windows and a walk-forward split need, and
      * anything that is not a company or a fund. Measured before this filter existed, the listing
        offered 1,984 trainable-looking non-investable instruments — 1,041 warrants, 577 the
        provider does not carry, 318 units and 48 rights. A model fitted to a SPAC warrant's price
        history is not a bad model, it is a category error.

    The investable set comes from the `investable_securities` VIEW rather than a hardcoded type
    list, so the Lab and the classifier cannot drift apart. The Lab image ships no db/ package, so
    a view is the only way it can share that definition rather than copy it (migration 027).
    """
    from lab.datasets import MIN_BARS

    try:
        with store.connection() as conn:
            rows = conn.execute(
                """
                SELECT s.symbol, s.security_type, count(*) AS bars,
                       min(b.trade_date), max(b.trade_date)
                  FROM price_bars_daily b
                  JOIN investable_securities s ON s.id = b.security_id
                 WHERE (%s::text IS NULL OR s.symbol LIKE upper(%s) || '%%')
                 GROUP BY s.symbol, s.security_type
                HAVING count(*) >= %s
                 ORDER BY count(*) DESC, s.symbol
                 LIMIT 100
                """,
                (q, q, MIN_BARS),
            ).fetchall()
    except Exception as exc:  # the Lab is useless without its database; say so plainly
        logger.error("dataset listing failed: %s", exc)
        raise HTTPException(status_code=503, detail="the Lab cannot reach its database") from exc

    return {
        "min_bars": MIN_BARS,
        "symbols": [
            {
                "symbol": r[0],
                # Reported, not just filtered on. A caller looking at a list of tickers should be
                # able to see that GLD is a fund and BRK.B is a share class without asking.
                "security_type": r[1],
                "bars": r[2],
                "start": str(r[3]),
                "end": str(r[4]),
            }
            for r in rows
        ],
    }


@app.post(f"{PREFIX}/experiments/run")
async def run_experiment(body: ExperimentRequest) -> StreamingResponse:
    """Train and walk-forward validate one or more models. Streams progress; ends with the run."""
    return _sse(
        runner.run_experiment(
            name=body.name,
            models=body.models,
            dataset_spec=body.dataset.model_dump(),
            params=body.params,
            validation=body.validation.model_dump(),
            operator=body.operator,
        )
    )


@app.post(f"{PREFIX}/sweeps")
async def run_sweep(body: SweepRequest) -> StreamingResponse:
    if len(body.values) > MAX_SWEEP_VALUES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"a sweep is capped at {MAX_SWEEP_VALUES} values; {len(body.values)} requested. "
                "Each value is a full walk-forward validation."
            ),
        )
    return _sse(
        runner.run_sweep(
            name=body.name,
            model=body.model,
            param=body.param,
            values=list(body.values),
            dataset_spec=body.dataset.model_dump(),
            metric=body.metric,
            validation=body.validation.model_dump(),
            operator=body.operator,
        )
    )


@app.get(f"{PREFIX}/experiments")
def list_experiments(limit: int = Query(50, ge=1, le=200)) -> dict:
    return {"experiments": store.list_experiments(limit)}


@app.get(f"{PREFIX}/experiments/{{experiment_id}}")
def get_experiment(experiment_id: int) -> dict:
    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="no such experiment")
    return experiment


@app.get(f"{PREFIX}/compare")
def compare(
    metric: str = Query("sharpe_ratio"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """The leaderboard: the best MEASURED run per model.

    Every row carries its data_source, because a ranking that does not say which entries came from
    generated data invites exactly the comparison it should prevent.
    """
    try:
        return store.leaderboard(metric=metric, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
