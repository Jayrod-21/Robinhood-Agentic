# Contract: `GET /api/performance` (issue #29)

The Performance page (`frontend/src/app/performance/page.tsx`) is built and renders today against a
dev fixture (`NEXT_PUBLIC_PERF_MOCK=1`, see `frontend/src/lib/perf.ts`). This is the one endpoint it
needs to go live. It is **read-only** — no new write paths, no broker access, no secrets.

Owner: backend workstream. The frontend consumes this shape verbatim; the TypeScript interfaces in
`frontend/src/lib/perf.ts` are the source of truth for field names and types.

## Sources

Everything comes from tables that already exist (migration `004_evaluation`):

- **Equity curve** ← `portfolio_returns_daily` (`trade_date`, `market_value`, `cumulative_return`)
  for the book being shown, ordered by `trade_date`.
- **Benchmark curve** ← the benchmark's cumulative return over the same dates. Two options, your
  call: (a) a benchmark `paper_portfolio` marked the same way, or (b) computed from
  `price_bars_daily` for `evaluation_runs.benchmark_security_id` (e.g. SPY), rebased to the book's
  inception. The page only needs `benchmark_cumulative_return` per date.
- **Metrics** ← the latest `evaluation_runs` row for the book (most recent `window_end`, prefer
  `walk_forward = 'live'`). Pass the columns straight through.

## Which book?

Default to the `real` book (`paper_portfolios.kind = 'real'`). Optional `?portfolio_id=` to select
another (e.g. an `agent_composite`) later — the page doesn't send it yet.

## Response

```jsonc
{
  "meta": {
    "portfolio_id": 1,
    "kind": "real",
    "inception_date": "2026-06-03",
    "benchmark_symbol": "SPY",          // null if none configured
    "priced_through": "2026-08-04",     // last trade_date with a mark; null if no marks
    "returns_basis": "price_only",      // "price_only" | "total_return" — do NOT report total
                                        // return unless dividends are actually loaded for both
                                        // the book and the benchmark (the schema already enforces
                                        // this distinction; the page surfaces it)
    "coverage": 1.0,                    // marks present / expected trading days in window (0..1),
                                        // or null if not computed
    "coverage_note": null               // e.g. "15 trading days of Dec-2024 missing"; null if clean
  },

  "equity_curve": [                     // ordered by trade_date ASC
    {
      "trade_date": "2026-06-03",
      "market_value": 100.00,           // portfolio_returns_daily.market_value (USD)
      "cumulative_return": null,        // fractional (0.0123 = +1.23%); null on the first mark
      "benchmark_cumulative_return": 0.0   // fractional; null if the benchmark had no price that day
    }
    // ...
  ],

  "metrics": {                          // null when < 2 marks exist (nothing computable yet)
    "window_start": "2026-06-03",
    "window_end": "2026-08-04",
    "n_observations": 45,               // the REAL mark count — never asserted
    "is_rankable": false,               // evaluation_runs.is_rankable (n >= min_n_for_ranking)
    "min_n_for_ranking": 60,            // the floor in force for this row
    "sharpe": 0.71,                     // annual; null if n < 2
    "sortino": 0.94,                    // annual; null if n < 2
    "max_drawdown": -0.052,             // fractional (negative)
    "hit_rate": 0.56,                   // fractional 0..1
    "volatility": 0.181,                // fractional, annualized
    "total_return": 0.041,              // fractional
    "annualized_return": 0.128,         // fractional
    "information_ratio": -0.14,         // vs the benchmark; null if n < 2
    "walk_forward": "live",             // "live" | "in_sample" | "out_of_sample"
    "risk_free_annual": 0.043           // the rate actually used (from risk_free_rates)
  }
}
```

## Degradation (matches the `history` router)

- **DB absent** → `503` with a clear, non-secret `detail`. The page already renders this as a calm
  "database isn't connected" state (not an error), same contract as `/api/history/*`.
- **No marks yet** → `200` with `equity_curve: []` and `metrics: null`. The page shows an "after the
  valuation job runs twice" empty state.

## Honesty fields — please don't drop these

`is_rankable` / `n_observations` / `min_n_for_ranking`, `returns_basis`, and `coverage` are what keep
the page from presenting a lucky 45-day Sharpe as a verdict. The UI renders a prominent "not yet
rankable" banner off `is_rankable`, a "price-only" note off `returns_basis`, and a coverage note when
`coverage < 1`. They're cheap to populate (all already in the schema) and they're the point.

## Frontend done / handoff

- [x] Page, nav entry, chart (value + return% toggle), headline stats, full scorecard, all states
- [x] Renders now under `NEXT_PUBLIC_PERF_MOCK=1`
- [ ] **backend:** implement this route → drop the flag → move the types from `lib/perf.ts` into
      `lib/types.ts` and delete the fixture
