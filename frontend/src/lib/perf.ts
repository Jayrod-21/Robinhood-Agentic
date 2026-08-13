// Types + a dev-only fixture for the Performance page (issue #29).
//
// These mirror the PROPOSED `GET /api/performance` contract documented in
// docs/contracts/performance-endpoint.md, which the backend workstream will implement over
// `portfolio_returns_daily` (the equity curve) and `evaluation_runs` (the risk metrics).
//
// Deliberately NOT in lib/types.ts: that file's invariant is "mirrors the shapes a real backend
// returns TODAY". This endpoint does not exist yet, so putting its shape there would break that
// promise. Move these across once the route ships, and delete the fixture below.

export interface EquityPoint {
  /** ISO date (YYYY-MM-DD) of the daily mark. */
  trade_date: string;
  /** Portfolio market value in USD on this day (portfolio_returns_daily.market_value). */
  market_value: number;
  /** Fractional cumulative return since inception (0.0123 = +1.23%). NULL on the first mark. */
  cumulative_return: number | null;
  /** Benchmark's fractional cumulative return over the same window; NULL when the benchmark
   *  couldn't be priced that day (a gap surfaced, not interpolated away). */
  benchmark_cumulative_return: number | null;
}

/** evaluation_runs.walk_forward — 'live' is honest production forward marking; the others label a
 *  score computed on data the strategy was fitted to, which must never read as an honest one. */
export type WalkForward = "live" | "in_sample" | "out_of_sample";

/** 'price_only' when dividends aren't loaded for the book or its benchmark. The schema refuses to
 *  call a price return a total return; so does this page. */
export type ReturnsBasis = "price_only" | "total_return";

export interface PerformanceMetrics {
  window_start: string;
  window_end: string;
  /** The real mark count behind these ratios — the sample size, never asserted. */
  n_observations: number;
  /** evaluation_runs.is_rankable = n_observations >= min_n_for_ranking. When false the ratios
   *  below exist but the record is too short to rank; the UI must say so, loudly. */
  is_rankable: boolean;
  min_n_for_ranking: number;
  // All ratios are annual; returns/drawdown/vol are FRACTIONAL (0.0123 = +1.23%). NULL means
  // arithmetically undefined (Sharpe/Sortino need n >= 2), rendered as "—", never as 0.
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number | null;
  hit_rate: number | null;
  volatility: number | null;
  total_return: number | null;
  annualized_return: number | null;
  information_ratio: number | null;
  walk_forward: WalkForward;
  risk_free_annual: number;
}

export interface PerformanceMeta {
  portfolio_id: number;
  kind: string; // 'real' | 'agent_composite' | 'counterfactual' | ...
  inception_date: string;
  benchmark_symbol: string | null; // e.g. 'SPY'
  priced_through: string | null; // last trade_date with a mark
  returns_basis: ReturnsBasis;
  /** Fraction of expected trading days in the window that actually carry a mark (0..1). < 1 means
   *  gaps (e.g. the corrupt Dec-2024 archive) — surfaced as a coverage ratio, never hidden. */
  coverage: number | null;
  coverage_note: string | null;
}

export interface PerformanceResponse {
  meta: PerformanceMeta;
  equity_curve: EquityPoint[];
  /** NULL until there are >= 2 marks to compute anything from. */
  metrics: PerformanceMetrics | null;
}

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Renders the page with no backend when NEXT_PUBLIC_PERF_MOCK=1. Strictly gated: with the flag
// unset (the default, and production) the page hits the real endpoint and shows honest empty /
// degraded states instead. A mock that silently stands in for a broken endpoint is exactly the
// self-deception this project exists to avoid, so the page also stamps a visible "MOCK" badge.
export const PERF_MOCK = process.env.NEXT_PUBLIC_PERF_MOCK === "1";

function businessDays(startISO: string, count: number): string[] {
  const out: string[] = [];
  const d = new Date(`${startISO}T00:00:00Z`);
  while (out.length < count) {
    const dow = d.getUTCDay();
    if (dow !== 0 && dow !== 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

// Deterministic (no randomness): a gentle drift plus a sine wobble, the book running a touch behind
// SPY — anchored to fixed dates so priced_through doesn't drift day to day. n=45 against a min of 60
// deliberately lands is_rankable=false, so the "too short to rank" path is what you see by default.
function buildMockPerformance(): PerformanceResponse {
  const dates = businessDays("2026-06-03", 45);
  const start = 100;
  const equity_curve: EquityPoint[] = dates.map((trade_date, i) => {
    const t = i / (dates.length - 1);
    const benchCum = 0.06 * t + 0.015 * Math.sin(i / 3);
    const portCum = 0.045 * t + 0.02 * Math.sin(i / 2.4 + 0.5) - 0.004;
    return {
      trade_date,
      market_value: Number((start * (1 + portCum)).toFixed(2)),
      cumulative_return: i === 0 ? null : Number(portCum.toFixed(6)),
      benchmark_cumulative_return: Number(benchCum.toFixed(6)),
    };
  });
  const last = equity_curve[equity_curve.length - 1];
  return {
    meta: {
      portfolio_id: 1,
      kind: "real",
      inception_date: dates[0],
      benchmark_symbol: "SPY",
      priced_through: last.trade_date,
      returns_basis: "price_only",
      coverage: 1,
      coverage_note: null,
    },
    equity_curve,
    metrics: {
      window_start: dates[0],
      window_end: last.trade_date,
      n_observations: 45,
      is_rankable: false,
      min_n_for_ranking: 60,
      sharpe: 0.71,
      sortino: 0.94,
      max_drawdown: -0.052,
      hit_rate: 0.56,
      volatility: 0.181,
      total_return: last.cumulative_return,
      annualized_return: 0.128,
      information_ratio: -0.14,
      walk_forward: "live",
      risk_free_annual: 0.043,
    },
  };
}

export const MOCK_PERFORMANCE: PerformanceResponse = buildMockPerformance();
