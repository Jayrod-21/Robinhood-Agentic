// Types plus a dev fixture for the Testing Lab: train and compare ML models (XGBoost, Random Forest,
// ElasticNet, ARIMA) with honest, no-lookahead validation and a model leaderboard. The backend ports
// the framework-agnostic ML lib from Special-Sprinkle-Sauce into src/ml/, runs it in a separate Lab
// container, and the trading backend aggregates it at GET /api/testing-lab.
//
// THESE SHAPES MIRROR THE REAL ENDPOINT, not a guess. Sources of truth:
//   - meta / experiments / comparison / stress: backend/app/routers/testing_lab.py::lab_overview
//   - leaderboard rows:                          lab/store.py::leaderboard
//   - experiment rows:                           lab/store.py::list_experiments (_experiment_dict)
//   - metrics blob:                              src/ml/validation.py::calculate_metrics
// The overview's `experiments` are experiment-level rows: they carry status/timing, NOT per-model
// metrics or walk-forward steps. Those live in model_runs and are only returned by the per-experiment
// detail route (GET /api/testing-lab/experiments/{id}); the leaderboard (Comparison tab) is where the
// measured per-model metrics surface.
//
// Honesty note carried through the UI: Sharpe / profit factor come from a +1/-1 directional P&L proxy
// (src/ml/validation.py), not realised returns, so accuracy and information coefficient are the honest
// signal; a MEASURED run can still be worthless (a constant predictor), which is what `degenerate`
// says, printed beside the number rather than hidden.

export type ModelKind = "xgboost" | "random_forest" | "elastic_net" | "arima";

const MODEL_LABELS: Record<string, string> = {
  xgboost: "XGBoost",
  random_forest: "Random Forest",
  elastic_net: "Elastic Net",
  arima: "ARIMA",
};

/** Backend `model_name` is a free string; label the ones we know, prettify the rest so a new model
 *  still renders a sensible name instead of a raw slug. */
export function modelLabel(model: string): string {
  return MODEL_LABELS[model] ?? model.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const DATA_SOURCE_LABELS: Record<string, string> = {
  synthetic: "Synthetic",
  dow_jones_1928: "Dow Jones 1928-2009",
  live_bars: "Live daily bars",
};

export function dataSourceLabel(source: string | null | undefined): string {
  if (!source) return "Unknown";
  return DATA_SOURCE_LABELS[source] ?? source.replace(/_/g, " ");
}

/** The metrics blob on a model run (src/ml/validation.py::calculate_metrics). `measured` is the field
 *  that gates the rest: false means nothing was scored, and every number below is a zero placeholder. */
export interface ModelMetrics {
  measured: boolean;
  accuracy: number;
  precision: number;
  recall: number;
  f1: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  /** Pearson corr of predicted probability vs realized direction. The honest signal-quality number. */
  information_coefficient: number;
  total_predictions: number;
}

/** The numeric metric keys, for typing the ranked-by field and the leaderboard columns. */
export type MetricKey = Exclude<keyof ModelMetrics, "measured">;

export interface LabMeta {
  /** A free string from the run that actually produced these numbers (e.g. "synthetic"), not a
   *  constant, so the page cannot claim "synthetic" while showing results fitted to real bars. */
  data_source: string;
  /** Every metric is from walk-forward test windows; the validator has no in-sample path. */
  out_of_sample: boolean;
  /** True while the P&L-based metrics use the +1/-1 directional proxy, not real returns. */
  proxy_pnl: boolean;
  generated_at: string;
  /** From the Lab's health check; shape is loose (a flag or status). Rendered defensively. */
  database?: boolean | string | null;
}

/** One row of GET /api/testing-lab (list_experiments). Experiment-LEVEL: it has no metrics of its own;
 *  a single experiment fans out into one or more model_runs, which the leaderboard ranks. */
export interface ExperimentRow {
  id: number;
  name: string;
  kind: string;
  data_source: string;
  dataset: string;
  params: Record<string, unknown>;
  validation_kind: string;
  /** e.g. "complete" | "failed" | "running"; kept a free string, the backend owns the vocabulary. */
  status: string;
  operator: string | null;
  created_at: string;
  completed_at: string | null;
  error: string | null;
}

/** One leaderboard row (lab/store.py::leaderboard): the best MEASURED run per model, with the honesty
 *  fields carried alongside so a ranking cannot present a constant predictor as a winner silently. */
export interface LeaderboardRow {
  model: string;
  run_id: number;
  metrics: ModelMetrics;
  predictions_made: number;
  predictions_failed: number;
  is_baseline: boolean;
  data_source: string;
  dataset: string;
  created_at: string;
  /** Ways a measured result can still be worthless, each written to be shown as-is. Empty is clean. */
  degenerate: string[];
}

export interface ComparisonResponse {
  /** The metric the leaderboard was ranked by (a key of ModelMetrics). */
  metric: string;
  models: LeaderboardRow[];
  /** Runs that scored nothing and were excluded from the ranking. Reported, not hidden: a Lab with
   *  forty failed runs and two good ones should not look like a Lab with two runs. */
  unmeasured_runs: number;
}

export interface StressScenario {
  key: string;
  label: string;
  period: string;
  spy_move_pct: number;
  /** Estimated portfolio P&L in the scenario, percent. From SSS stress_test.py sector multipliers. */
  estimated_pl_pct: number;
  worst_sector: string;
}

export interface StressResponse {
  /** False until the stress module is ported; the page shows a calm "not yet" state, not fabricated
   *  scenarios (an empty list rendered as rows would read as "no risk"). */
  available: boolean;
  scenarios: StressScenario[];
  note?: string;
  generated_at?: string;
}

export interface TestingLabResponse {
  meta: LabMeta;
  experiments: ExperimentRow[];
  comparison: ComparisonResponse;
  stress: StressResponse;
}

// ── Dev fixture ──────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_TESTLAB_MOCK=1); the default hits the real endpoint. Shaped to match the
// real payload exactly (so this is also how the reconciled page is verified without a live Lab).
// Numbers are deliberately honest: models on synthetic data hover just above coin-flip, one is below
// it, and elastic_net is the real first-run failure mode: a constant "up" predictor that topped the
// Sharpe board with a NEGATIVE information coefficient, flagged as degenerate.
export const TESTLAB_MOCK = process.env.NEXT_PUBLIC_TESTLAB_MOCK === "1";

const GENERATED_AT = "2026-08-28T20:00:00Z";

function metrics(m: Partial<ModelMetrics> & { accuracy: number; information_coefficient: number }): ModelMetrics {
  const acc = m.accuracy;
  return {
    measured: true,
    accuracy: acc,
    precision: m.precision ?? Number((acc - 0.01).toFixed(4)),
    recall: m.recall ?? Number((acc + 0.02).toFixed(4)),
    f1: m.f1 ?? Number((acc + 0.005).toFixed(4)),
    sharpe_ratio: m.sharpe_ratio ?? Number(((acc - 0.5) * 12).toFixed(4)),
    max_drawdown: m.max_drawdown ?? Number((-(1 - acc) * 40).toFixed(4)),
    win_rate: m.win_rate ?? acc,
    profit_factor: m.profit_factor ?? Number((acc / (1 - acc)).toFixed(4)),
    information_coefficient: m.information_coefficient,
    total_predictions: m.total_predictions ?? 475,
  };
}

const LEADERBOARD: LeaderboardRow[] = [
  {
    model: "xgboost",
    run_id: 41,
    metrics: metrics({ accuracy: 0.573, information_coefficient: 0.121 }),
    predictions_made: 475,
    predictions_failed: 0,
    is_baseline: false,
    data_source: "synthetic",
    dataset: "synthetic",
    created_at: "2026-08-28T18:31:00Z",
    degenerate: [],
  },
  {
    model: "random_forest",
    run_id: 39,
    metrics: metrics({ accuracy: 0.558, information_coefficient: 0.094 }),
    predictions_made: 475,
    predictions_failed: 0,
    is_baseline: false,
    data_source: "synthetic",
    dataset: "synthetic",
    created_at: "2026-08-28T18:22:00Z",
    degenerate: [],
  },
  {
    model: "elastic_net",
    run_id: 37,
    // The first-run failure: top Sharpe, negative IC, because it called "up" on every day.
    metrics: metrics({ accuracy: 0.512, information_coefficient: -0.13, sharpe_ratio: 1.58, recall: 1.0, win_rate: 0.512 }),
    predictions_made: 475,
    predictions_failed: 0,
    is_baseline: true,
    data_source: "synthetic",
    dataset: "synthetic",
    created_at: "2026-08-28T18:10:00Z",
    degenerate: [
      "predicted a single direction (up) on all 475 predictions: recall 1.0, IC negative; its Sharpe is the market's, not the model's",
    ],
  },
  {
    model: "arima",
    run_id: 35,
    metrics: metrics({ accuracy: 0.487, information_coefficient: -0.018 }),
    predictions_made: 452,
    predictions_failed: 23,
    is_baseline: false,
    data_source: "synthetic",
    dataset: "synthetic",
    created_at: "2026-08-28T17:58:00Z",
    degenerate: [],
  },
];

const EXPERIMENTS: ExperimentRow[] = [
  {
    id: 12, name: "synthetic walk-forward sweep", kind: "single", data_source: "synthetic", dataset: "synthetic",
    params: { models: "xgboost, random_forest, elastic_net, arima", horizon: 5 },
    validation_kind: "walk_forward", status: "complete", operator: "joe",
    created_at: "2026-08-28T18:05:00Z", completed_at: "2026-08-28T18:32:00Z", error: null,
  },
  {
    id: 11, name: "arima convergence retry", kind: "single", data_source: "synthetic", dataset: "synthetic",
    params: { model: "arima", order: "(2,1,2)" },
    validation_kind: "walk_forward", status: "failed", operator: "joe",
    created_at: "2026-08-28T17:40:00Z", completed_at: "2026-08-28T17:41:00Z",
    error: "statsmodels failed to converge on 23 of 475 windows; run scored the rest and flagged the gaps",
  },
  {
    id: 10, name: "dow-jones baseline", kind: "single", data_source: "dow_jones_1928", dataset: "dow_jones_1928",
    params: { models: "xgboost, elastic_net" },
    validation_kind: "cross_validation", status: "complete", operator: "jared",
    created_at: "2026-08-27T22:15:00Z", completed_at: "2026-08-27T22:39:00Z", error: null,
  },
];

export const MOCK_TESTING_LAB: TestingLabResponse = {
  meta: { data_source: "synthetic", out_of_sample: true, proxy_pnl: true, generated_at: GENERATED_AT, database: true },
  experiments: EXPERIMENTS,
  comparison: { metric: "information_coefficient", models: LEADERBOARD, unmeasured_runs: 2 },
  stress: { available: false, scenarios: [], note: "stress scenarios are not implemented yet" },
};
