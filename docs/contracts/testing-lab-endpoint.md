# Contract: `GET /api/testing-lab` (+ run/sweep endpoints)

Feeds the Testing Lab page (`frontend/src/app/testing-lab/page.tsx`), built and rendering today
against a dev fixture (`NEXT_PUBLIC_TESTLAB_MOCK=1`, see `frontend/src/lib/testingLab.ts`). This is
Phase D/G of the roadmap and is greenfield in this app. **Read-only** for the GET; the run/sweep
endpoints kick off backend ML work but touch no broker or money. TypeScript interfaces in
`frontend/src/lib/testingLab.ts` are the source of truth for shapes.

## Where the code comes from

Most of this is a **port from the Special-Sprinkle-Sauce repo** (`/home/joe/Special-Sprinkle-Sauce`),
not a fresh build. See `Downloads/wasden-watch-backend-plan-for-jared.md` for the full port list. The
key one: the metric set below is **exactly** what SSS `src/intelligence/quant_models/validation.py::calculate_metrics()`
returns, so if you port that function the frontend and backend agree by construction.

## `GET /api/testing-lab` (page load)

Returns the recent experiments, the comparison leaderboard, and the stress-scenario results in one
payload so the page is one fetch.

```jsonc
{
  "meta": {
    "data_source": "synthetic",     // synthetic | dow_jones_1928 | live_bars
    "out_of_sample": true,          // all metrics are holdout / walk-forward test windows only
    "proxy_pnl": true,              // Sharpe/PF use the +1/-1 directional proxy until real returns
    "generated_at": "2026-08-19T20:00:00Z"
  },
  "experiments": [
    {
      "id": "exp_xgboost_2026-08-19", "created_at": "...",
      "model": "xgboost",            // xgboost | random_forest | elastic_net | arima
      "dataset": "synthetic", "validation_kind": "walk_forward",  // walk_forward | cross_validation
      "status": "complete",          // queued | running | complete | failed
      "is_baseline": false,
      "params": { "n_estimators": 300, "max_depth": 4, "learning_rate": 0.05 },
      "n_features": 12,
      "metrics": {                   // null while queued/running or on failure
        "accuracy": 0.573, "precision": 0.563, "recall": 0.593, "f1": 0.578,
        "sharpe_ratio": 0.88, "max_drawdown": -17.08, "win_rate": 0.573,
        "profit_factor": 1.34, "information_coefficient": 0.121, "total_predictions": 1260
      },
      "steps": [ { "step": 1, "train_size": 252, "test_start": "2019-01-02", "test_end": "2019-06-28", "accuracy": 0.543 } ],
      "lookahead_guarded": true,     // true ONLY when every train window ends strictly before its test window
      "notes": null
    }
  ],
  "comparison": {
    "rows": [ { "model": "xgboost", "metrics": { ... }, "is_best": true } ],
    "disagreement": { "mean_pairwise_agreement_pct": 61.4, "unanimous_pct": 22.8, "high_disagreement_flag": false },
    "ranked_by": "information_coefficient"
  },
  "stress": {
    "generated_at": "...",
    "scenarios": [
      { "key": "covid_2020", "label": "COVID crash", "period": "Feb-Mar 2020",
        "spy_move_pct": -33.9, "estimated_pl_pct": -21.4, "worst_sector": "Energy" }
    ]
  }
}
```

The `metrics` object is the SSS `calculate_metrics()` return, field-for-field (`win_rate == accuracy`
for directional models). The stress scenarios come from SSS `backend/app/services/risk/stress_test.py`
(6 named crashes with per-sector multipliers applied to the current allocation).

## Run + sweep endpoints (the page references these; wire when ready)

- `POST /api/testing-lab/experiments/run` (SSE): body `{ model, dataset, params, validation }`. Stream
  progress via the existing `backend/app/sse.py::sse_response` (same as the debate stream), end with the
  finished experiment record. Rate-limit via `backend/app/ratelimit.py`. Run async, never block the request.
- `POST /api/testing-lab/sweeps`: body `{ model, param, values }` → `{ id, model, param, points: [{value, sharpe_ratio, win_rate, max_drawdown}], best: {value, metric, score} }`. The page draws the curve and marks `best`.
- `GET /api/testing-lab/experiments` / `GET /api/testing-lab/experiments/{id}` for the run history.

## Honesty fields, please keep them

`out_of_sample`, `proxy_pnl`, `lookahead_guarded`, `data_source`, and a **negative** `information_coefficient`
when a model is worse than a coin flip. The page leans on all of these: it shows the proxy-P&L caveat, a
lookahead badge, a "synthetic, not live" flag, and renders a losing model losing (ARIMA at IC -0.018 in the
mock). A Testing Lab that only ever shows winners is lying; these fields are how it stays honest.

## Degradation

- **Backend not built yet** (`404`/`501`) or unavailable (`503`) → the page shows a calm "backend isn't
  available yet" state, not an error. No stub needed to avoid a scary banner.
- A `queued`/`running`/`failed` experiment carries `metrics: null`; the table renders em-dash placeholders.

## Data strategy (per the plan)

- Stage 1: run on synthetic OHLCV (`generate_mock_*`) + the public Dow Jones 1928-2009 loader in SSS
  `train_pipeline.py`, so the loop works with no live data pipeline.
- Stage 2: wire to real historical daily bars (bar archive / FMP), preserving the no-lookahead guarantees.

## Frontend done / handoff

- [x] Route `/testing-lab` + nav entry (Testing Lab, flask icon)
- [x] Tabs: Experiments (runs table + walk-forward accuracy chart), Comparison (leaderboard + IC bar + disagreement), Sweeps (curve + best marker), Stress (scenario table + SPY-vs-portfolio bars)
- [x] Honesty strip + proxy-P&L caveat; renders now under `NEXT_PUBLIC_TESTLAB_MOCK=1`
- [x] `NEXT_PUBLIC_TESTLAB_MOCK` registered in `ANY_MOCK` (`lib/dataTrust.ts`) so the trust strip warns while mock
- [ ] **backend:** implement these routes (port the SSS lib) → drop the flag → move types from `lib/testingLab.ts` into `lib/types.ts`
- [ ] **decision:** ML deps (xgboost/scikit-learn/statsmodels/joblib) in the main backend image vs a separate Lab service (changes where the frontend points)
