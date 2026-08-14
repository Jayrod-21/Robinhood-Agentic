// Types + a dev-only fixture for the Calibration page (decision track record).
//
// Mirrors the PROPOSED `GET /api/calibration` contract in
// docs/contracts/calibration-endpoint.md, which the backend computes from stated confidence
// (`judgments.confidence` for the jury, `agent_proposals.confidence` for the personas) paired with
// the realized outcome of each decision (EVALUATION_FRAMEWORK §3.2/§3.3 — every proposal becomes a
// marked paper portfolio, so losing calls are scored too, not just realized trades).
//
// Not in lib/types.ts on purpose (same reason as lib/perf.ts): that file mirrors shapes a real
// backend returns TODAY, and this endpoint doesn't exist yet. Move these over once it ships.

import type { ReturnsBasis } from "@/lib/perf";

/** Which stated-confidence source is being scored. */
export type CalibrationScope = "jury" | "personas";

/** One confidence bucket of a reliability diagram. `predicted` is the mean stated confidence of the
 *  decisions that fell in [lo, hi); `hit_rate` is the fraction of them that turned out correct.
 *  Perfect calibration is hit_rate == predicted (the diagonal). */
export interface CalibrationBin {
  lo: number; // 0..1
  hi: number; // 0..1
  predicted: number | null; // mean confidence in the bucket; null when n === 0
  n: number;
  hit_rate: number | null; // realized fraction correct; null when n === 0
}

export interface CalibrationSummary {
  n_decisions: number; // total scored decisions behind the curve
  /** Below this the curve is not meaningful; the page shows a "not yet calibratable" banner and
   *  never presents an ECE as a verdict. Same honesty gate as #29's is_rankable. */
  min_n_for_calibration: number;
  is_calibratable: boolean;
  /** Expected Calibration Error — n-weighted mean |predicted - hit_rate| across bins. Null below
   *  the gate. Lower is better; 0 is perfectly calibrated. */
  ece: number | null;
  /** Mean Brier score of the individual decisions. Null below the gate. */
  brier: number | null;
  /** Overall realized hit rate (the base rate). */
  base_rate: number | null;
  /** Mean stated confidence. base_rate < mean_confidence ⇒ systematically OVERCONFIDENT (the
   *  common failure); the page reads the gap out explicitly. */
  mean_confidence: number | null;
  bins: CalibrationBin[];
}

/** Per-agent record — the §3.2 judge / §3.3 persona calibration record made visible. Carries both
 *  the confidence-calibration error AND the realized risk-adjusted score of that agent's own
 *  counterfactual book, because a well-calibrated agent that still loses money is a different thing
 *  from one that makes it. */
export interface AgentCalibration {
  agent_id: number;
  name: string;
  kind: string; // 'judge' | 'bull' | 'bear' | 'wasden' | ...
  n: number;
  ece: number | null;
  mean_confidence: number | null;
  hit_rate: number | null;
  /** Realized from this agent's counterfactual paper portfolio (§3.3). Null until it has >= 2 marks. */
  sharpe: number | null;
  sortino: number | null;
}

/** One scored decision, for the drilldown table. */
export interface ScoredDecision {
  debate_id: number;
  ticker: string | null;
  created_at: string;
  agent: string;
  confidence: number;
  decision: string; // 'buy' | 'sell' | 'hold' | ...
  /** Null when the outcome isn't resolved yet (horizon not elapsed). */
  correct: boolean | null;
  /** The realized/counterfactual return used to score it; null if unresolved. */
  realized_pct: number | null;
}

export interface CalibrationMeta {
  scope: CalibrationScope;
  /** Human string of exactly how "correct" was decided — this choice drives the whole chart, so
   *  it is stated, never implied. e.g. "counterfactual return positive over 5 trading days". */
  outcome_definition: string;
  outcome_horizon_days: number | null;
  /** True when "correct" means beat the benchmark; false when it means positive return. */
  benchmark_relative: boolean;
  returns_basis: ReturnsBasis;
  priced_through: string | null;
  coverage: number | null;
  coverage_note: string | null;
}

export interface CalibrationResponse {
  meta: CalibrationMeta;
  overall: CalibrationSummary;
  by_agent: AgentCalibration[];
  decisions: ScoredDecision[];
}

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Same strict gate as perf.ts: only when NEXT_PUBLIC_CALIB_MOCK=1; the page stamps a MOCK badge and
// otherwise hits the real endpoint with honest empty/degraded states. Unlike #29 the fixture shows a
// POPULATED curve (n=64) so the reliability diagram is reviewable — with today's ~5 real debates the
// live page shows the "not yet calibratable" banner instead, which is the honest current state.
export const CALIB_MOCK = process.env.NEXT_PUBLIC_CALIB_MOCK === "1";

// A mildly OVERCONFIDENT book: at high stated confidence the realized hit rate sags below the
// diagonal. Deterministic, hand-set so the numbers reconcile (base_rate ≈ 0.55, mean_conf ≈ 0.67,
// ECE ≈ 0.14 — an overconfidence gap of ~12 points).
const JURY_BINS: CalibrationBin[] = [
  { lo: 0.0, hi: 0.1, predicted: null, n: 0, hit_rate: null },
  { lo: 0.1, hi: 0.2, predicted: 0.15, n: 1, hit_rate: 0.0 },
  { lo: 0.2, hi: 0.3, predicted: 0.25, n: 2, hit_rate: 0.5 },
  { lo: 0.3, hi: 0.4, predicted: 0.35, n: 3, hit_rate: 0.33 },
  { lo: 0.4, hi: 0.5, predicted: 0.45, n: 5, hit_rate: 0.4 },
  { lo: 0.5, hi: 0.6, predicted: 0.55, n: 9, hit_rate: 0.56 },
  { lo: 0.6, hi: 0.7, predicted: 0.65, n: 13, hit_rate: 0.54 },
  { lo: 0.7, hi: 0.8, predicted: 0.75, n: 15, hit_rate: 0.6 },
  { lo: 0.8, hi: 0.9, predicted: 0.85, n: 11, hit_rate: 0.64 },
  { lo: 0.9, hi: 1.0, predicted: 0.95, n: 5, hit_rate: 0.6 },
];

function scopeResponse(scope: CalibrationScope): CalibrationResponse {
  const jury = scope === "jury";
  return {
    meta: {
      scope,
      outcome_definition: "counterfactual return positive over 5 trading days",
      outcome_horizon_days: 5,
      benchmark_relative: false,
      returns_basis: "price_only",
      priced_through: "2026-08-04",
      coverage: 1,
      coverage_note: null,
    },
    overall: {
      n_decisions: 64,
      min_n_for_calibration: 30,
      is_calibratable: true,
      ece: 0.14,
      brier: 0.23,
      base_rate: 0.55,
      mean_confidence: 0.67,
      bins: JURY_BINS,
    },
    by_agent: jury
      ? [
          { agent_id: 1, name: "Value juror", kind: "judge", n: 16, ece: 0.09, mean_confidence: 0.63, hit_rate: 0.58, sharpe: 0.88, sortino: 1.12 },
          { agent_id: 2, name: "Momentum juror", kind: "judge", n: 18, ece: 0.17, mean_confidence: 0.74, hit_rate: 0.55, sharpe: 0.61, sortino: 0.74 },
          { agent_id: 3, name: "Risk juror", kind: "judge", n: 14, ece: 0.08, mean_confidence: 0.6, hit_rate: 0.57, sharpe: 0.95, sortino: 1.2 },
          { agent_id: 4, name: "Macro juror", kind: "judge", n: 16, ece: 0.15, mean_confidence: 0.71, hit_rate: 0.5, sharpe: 0.4, sortino: 0.51 },
        ]
      : [
          { agent_id: 11, name: "Bull (optimist)", kind: "bull", n: 22, ece: 0.19, mean_confidence: 0.78, hit_rate: 0.54, sharpe: 0.72, sortino: 0.9 },
          { agent_id: 12, name: "Bear (pessimist)", kind: "bear", n: 22, ece: 0.11, mean_confidence: 0.62, hit_rate: 0.56, sharpe: 0.83, sortino: 1.05 },
          { agent_id: 13, name: "Wasden lens", kind: "wasden", n: 20, ece: 0.07, mean_confidence: 0.6, hit_rate: 0.58, sharpe: 1.02, sortino: 1.31 },
        ],
    decisions: [
      { debate_id: 41, ticker: "TSM", created_at: "2026-08-01", agent: jury ? "Momentum juror" : "Bull (optimist)", confidence: 0.86, decision: "buy", correct: false, realized_pct: -0.021 },
      { debate_id: 40, ticker: "NVDA", created_at: "2026-07-30", agent: jury ? "Value juror" : "Wasden lens", confidence: 0.74, decision: "buy", correct: true, realized_pct: 0.038 },
      { debate_id: 39, ticker: "META", created_at: "2026-07-28", agent: jury ? "Risk juror" : "Bear (pessimist)", confidence: 0.58, decision: "hold", correct: true, realized_pct: 0.004 },
      { debate_id: 38, ticker: "AVGO", created_at: "2026-07-25", agent: jury ? "Macro juror" : "Bull (optimist)", confidence: 0.81, decision: "buy", correct: false, realized_pct: -0.013 },
      { debate_id: 37, ticker: "COST", created_at: "2026-07-22", agent: jury ? "Value juror" : "Wasden lens", confidence: 0.69, decision: "buy", correct: true, realized_pct: 0.052 },
      { debate_id: 36, ticker: "LLY", created_at: "2026-07-18", agent: jury ? "Momentum juror" : "Bear (pessimist)", confidence: 0.92, decision: "buy", correct: false, realized_pct: -0.007 },
      { debate_id: 35, ticker: "V", created_at: "2026-07-15", agent: jury ? "Risk juror" : "Bull (optimist)", confidence: 0.45, decision: "hold", correct: false, realized_pct: -0.018 },
      { debate_id: 34, ticker: "UNH", created_at: "2026-07-11", agent: jury ? "Macro juror" : "Wasden lens", confidence: 0.63, decision: "sell", correct: true, realized_pct: -0.026 },
    ],
  };
}

export function mockCalibration(scope: CalibrationScope): CalibrationResponse {
  return scopeResponse(scope);
}
