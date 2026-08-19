// Types plus a dev-only fixture for the Testing Lab: train and compare ML models (XGBoost, Random
// Forest, ElasticNet, ARIMA) with honest, no-lookahead validation, parameter sweeps, crisis-scenario
// stress tests, and a model leaderboard. This is Phase D/G of the roadmap, and it is greenfield in
// this app; the backend ports the framework-agnostic ML lib from the Special-Sprinkle-Sauce repo
// (src/intelligence/quant_models/), so these shapes mirror that code's real outputs.
//
// The metric set is EXACTLY what SSS validation.calculate_metrics() returns, so the frontend and the
// ported backend agree by construction. Contract: docs/contracts/testing-lab-endpoint.md.
//
// Honesty note carried through the UI: the directional models score win_rate == accuracy, and the
// Sharpe / profit-factor come from a simplified +1/-1 per-call P&L proxy until real return data is
// wired (v1b). That caveat is surfaced on the page, never hidden behind a clean-looking number.

export type ModelKind = "xgboost" | "random_forest" | "elastic_net" | "arima";
export type ValidationKind = "walk_forward" | "cross_validation";
export type ExperimentStatus = "queued" | "running" | "complete" | "failed";
export type DatasetKind = "synthetic" | "dow_jones_1928" | "live_bars";

export const MODEL_LABEL: Record<ModelKind, string> = {
  xgboost: "XGBoost",
  random_forest: "Random Forest",
  elastic_net: "Elastic Net",
  arima: "ARIMA",
};

export const DATASET_LABEL: Record<DatasetKind, string> = {
  synthetic: "Synthetic",
  dow_jones_1928: "Dow Jones 1928-2009",
  live_bars: "Live daily bars",
};

/** Mirrors SSS validation.calculate_metrics() exactly. Directional models: win_rate == accuracy. */
export interface ModelMetrics {
  accuracy: number;
  precision: number;
  recall: number;
  f1: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  /** Pearson corr of predicted probability vs realized label. The honest signal-quality number. */
  information_coefficient: number;
  total_predictions: number;
}

/** One expanding-window walk-forward step; the per-step accuracy is what the equity-of-skill chart plots. */
export interface WalkForwardStep {
  step: number;
  train_size: number;
  test_start: string;
  test_end: string;
  accuracy: number;
}

export interface Experiment {
  id: string;
  created_at: string;
  model: ModelKind;
  dataset: DatasetKind;
  validation_kind: ValidationKind;
  status: ExperimentStatus;
  is_baseline: boolean;
  params: Record<string, number | string>;
  n_features: number;
  /** null while queued/running or on failure. */
  metrics: ModelMetrics | null;
  steps: WalkForwardStep[];
  /** True only when every train window ends strictly before its test window (the no-lookahead gate). */
  lookahead_guarded: boolean;
  notes: string | null;
}

export interface LeaderboardRow {
  model: ModelKind;
  metrics: ModelMetrics;
  is_best: boolean;
}

export interface DisagreementSummary {
  /** Mean pairwise agreement across models on the same test set, percent. */
  mean_pairwise_agreement_pct: number;
  /** Share of test points where every model agreed on direction. */
  unanimous_pct: number;
  /** True when models disagree enough that the composite should not be trusted blind. */
  high_disagreement_flag: boolean;
}

export interface ComparisonResponse {
  rows: LeaderboardRow[];
  disagreement: DisagreementSummary;
  /** The metric the leaderboard is ranked by (default information_coefficient). */
  ranked_by: keyof ModelMetrics;
}

export interface SweepPoint {
  value: number;
  sharpe_ratio: number;
  win_rate: number;
  max_drawdown: number;
}

export interface Sweep {
  id: string;
  model: ModelKind;
  param: string;
  points: SweepPoint[];
  /** The best point and the metric it was chosen on; the UI marks it on the curve. */
  best: { value: number; metric: keyof ModelMetrics; score: number };
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
  scenarios: StressScenario[];
  generated_at: string;
}

export interface TestingLabResponse {
  meta: {
    data_source: DatasetKind;
    /** All metrics are out-of-sample (holdout / walk-forward test windows only). */
    out_of_sample: boolean;
    /** True while the P&L-based metrics use the +1/-1 directional proxy, not real returns. */
    proxy_pnl: boolean;
    generated_at: string;
  };
  experiments: Experiment[];
  comparison: ComparisonResponse;
  stress: StressResponse;
}

// ── Dev fixture ──────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_TESTLAB_MOCK=1); the default hits the real endpoints. Numbers are
// deliberately modest and honest: directional models on synthetic data hover just above coin-flip
// (accuracy ~0.52-0.58), ICs are small, and one model is deliberately worse than a coin flip, because
// a Testing Lab that only ever shows winners is lying.
export const TESTLAB_MOCK = process.env.NEXT_PUBLIC_TESTLAB_MOCK === "1";

const GENERATED_AT = "2026-08-19T20:00:00Z";

// Deterministic per-model metrics (no Math.random in a fixture: it would hydrate-mismatch).
function metrics(m: Partial<ModelMetrics> & { accuracy: number; information_coefficient: number }): ModelMetrics {
  const acc = m.accuracy;
  return {
    accuracy: acc,
    precision: m.precision ?? Number((acc - 0.01).toFixed(4)),
    recall: m.recall ?? Number((acc + 0.02).toFixed(4)),
    f1: m.f1 ?? Number((acc + 0.005).toFixed(4)),
    sharpe_ratio: m.sharpe_ratio ?? Number(((acc - 0.5) * 12).toFixed(4)),
    max_drawdown: m.max_drawdown ?? Number((-(1 - acc) * 40).toFixed(4)),
    win_rate: acc, // directional models: win_rate == accuracy
    profit_factor: m.profit_factor ?? Number((acc / (1 - acc)).toFixed(4)),
    information_coefficient: m.information_coefficient,
    total_predictions: m.total_predictions ?? 1260,
  };
}

const MODEL_METRICS: Record<ModelKind, ModelMetrics> = {
  xgboost: metrics({ accuracy: 0.573, information_coefficient: 0.121 }),
  random_forest: metrics({ accuracy: 0.558, information_coefficient: 0.094 }),
  elastic_net: metrics({ accuracy: 0.541, information_coefficient: 0.061 }),
  arima: metrics({ accuracy: 0.487, information_coefficient: -0.018 }), // deliberately sub-coin-flip
};

function walkForwardSteps(baseAcc: number): WalkForwardStep[] {
  // 8 expanding-window steps, test windows strictly increasing in time (no lookahead).
  const wobble = [-0.03, 0.01, -0.05, 0.04, 0.0, -0.02, 0.03, 0.015];
  return wobble.map((w, i) => ({
    step: i + 1,
    train_size: 252 + i * 126,
    test_start: `20${19 + i}-01-02`,
    test_end: `20${19 + i}-06-28`,
    accuracy: Number(Math.max(0.35, Math.min(0.7, baseAcc + w)).toFixed(4)),
  }));
}

const EXPERIMENTS: Experiment[] = (["xgboost", "random_forest", "elastic_net", "arima"] as ModelKind[]).map(
  (model, i): Experiment => ({
    id: `exp_${model}_2026-08-19`,
    created_at: `2026-08-19T1${8 - i}:30:00Z`,
    model,
    dataset: "synthetic",
    validation_kind: "walk_forward",
    status: "complete",
    is_baseline: model === "elastic_net",
    params:
      model === "xgboost"
        ? { n_estimators: 300, max_depth: 4, learning_rate: 0.05 }
        : model === "random_forest"
          ? { n_estimators: 400, max_depth: 8 }
          : model === "elastic_net"
            ? { alpha: 0.1, l1_ratio: 0.5 }
            : { order: "(2,1,2)" },
    n_features: 12,
    metrics: MODEL_METRICS[model],
    steps: walkForwardSteps(MODEL_METRICS[model].accuracy),
    lookahead_guarded: true,
    notes: model === "arima" ? "Below coin-flip on this synthetic set; kept for honest comparison." : null,
  }),
);

const COMPARISON: ComparisonResponse = {
  rows: (["xgboost", "random_forest", "elastic_net", "arima"] as ModelKind[]).map((model) => ({
    model,
    metrics: MODEL_METRICS[model],
    is_best: model === "xgboost",
  })),
  disagreement: {
    mean_pairwise_agreement_pct: 61.4,
    unanimous_pct: 22.8,
    high_disagreement_flag: false,
  },
  ranked_by: "information_coefficient",
};

const STRESS: StressResponse = {
  generated_at: GENERATED_AT,
  scenarios: [
    { key: "covid_2020", label: "COVID crash", period: "Feb-Mar 2020", spy_move_pct: -33.9, estimated_pl_pct: -21.4, worst_sector: "Energy" },
    { key: "bear_2022", label: "2022 bear market", period: "Jan-Oct 2022", spy_move_pct: -25.4, estimated_pl_pct: -18.7, worst_sector: "Technology" },
    { key: "banking_2023", label: "Regional banking crisis", period: "Mar 2023", spy_move_pct: -7.8, estimated_pl_pct: -6.1, worst_sector: "Financials" },
    { key: "black_monday_1987", label: "Black Monday", period: "Oct 19, 1987", spy_move_pct: -20.5, estimated_pl_pct: -19.2, worst_sector: "Technology" },
    { key: "gfc_2008", label: "2008 financial crisis", period: "Sep-Nov 2008", spy_move_pct: -56.8, estimated_pl_pct: -41.0, worst_sector: "Financials" },
    { key: "crash_1929", label: "1929 crash", period: "Oct-Nov 1929", spy_move_pct: -47.9, estimated_pl_pct: -39.5, worst_sector: "Industrials" },
  ],
};

export const MOCK_TESTING_LAB: TestingLabResponse = {
  meta: { data_source: "synthetic", out_of_sample: true, proxy_pnl: true, generated_at: GENERATED_AT },
  experiments: EXPERIMENTS,
  comparison: COMPARISON,
  stress: STRESS,
};

/** A mock parameter sweep for one model/param, with an inverted-U so a real-looking optimum exists. */
export function mockSweep(model: ModelKind = "xgboost", param = "max_depth"): Sweep {
  const values = [2, 3, 4, 5, 6, 8, 10, 12];
  const peak = 4;
  const points: SweepPoint[] = values.map((v) => {
    const dist = Math.abs(v - peak);
    const sharpe = Number((0.9 - dist * 0.12).toFixed(4));
    return {
      value: v,
      sharpe_ratio: sharpe,
      win_rate: Number((0.575 - dist * 0.012).toFixed(4)),
      max_drawdown: Number((-14 - dist * 1.4).toFixed(4)),
    };
  });
  return { id: `sweep_${model}_${param}`, model, param, points, best: { value: peak, metric: "sharpe_ratio", score: 0.9 } };
}
