# Contract: `GET /api/calibration` (decision track record)

The Calibration page (`frontend/src/app/calibration/page.tsx`) is built and renders today against a
dev fixture (`NEXT_PUBLIC_CALIB_MOCK=1`, see `frontend/src/lib/calibration.ts`). This one endpoint
takes it live. **Read-only** — no writes, no broker, no secrets.

It answers the project's sharpest question: *when the jury said 80% confident, was it right 80% of the
time?* And it makes the framework's §3.2 (per-judge) and §3.3 (per-persona) calibration records
visible. TypeScript interfaces in `frontend/src/lib/calibration.ts` are the source of truth for shapes.

## Query

`?scope=jury` (default) or `?scope=personas` — which stated-confidence source to score:

- **jury** → `judgments.confidence` + `judgments.decision`, one row per judge per debate.
- **personas** → `agent_proposals.confidence` + `agent_proposals.stance`, one row per persona per debate.

The page sends `scope`; nothing else.

## Sources & the one judgement call: scoring "correct"

The hard part isn't the query, it's defining the realized outcome. The framework already points the
way (§3.3): **every proposal/ruling becomes a marked paper portfolio**, so a decision's outcome is
that book's realized return over a horizon — losing calls get scored too, which is what gives this
page enough observations to mean anything.

- **Confidence** ← `judgments.confidence` / `agent_proposals.confidence` (both `NUMERIC(5,4)`, 0..1).
- **Outcome** ← the decision's resulting/counterfactual `paper_portfolio` → `portfolio_returns_daily`
  over `outcome_horizon_days`. A `buy`/`sell` is **correct** when the directional call paid off
  (positive counterfactual return for a buy; avoided/negative for a sell); a `hold` when the name
  didn't move materially. State the exact rule you implement back in `meta.outcome_definition` — the
  page prints it verbatim so the reader always knows what "correct" meant.
- **Per-agent Sharpe/Sortino** ← that agent's counterfactual book's `evaluation_runs` (§3.3).

Decisions whose horizon hasn't elapsed are **unresolved** — return them with `correct: null` /
`realized_pct: null` (the page shows them as "pending"), and **exclude them from the bins and ECE**.

## Response

```jsonc
{
  "meta": {
    "scope": "jury",
    "outcome_definition": "counterfactual return positive over 5 trading days",
    "outcome_horizon_days": 5,
    "benchmark_relative": false,     // true if "correct" means beat SPY, not just positive
    "returns_basis": "price_only",   // same honesty flag as the performance endpoint
    "priced_through": "2026-08-04",
    "coverage": 1.0,
    "coverage_note": null
  },

  "overall": {
    "n_decisions": 64,               // RESOLVED decisions only
    "min_n_for_calibration": 30,     // below this the curve isn't meaningful
    "is_calibratable": false,        // n_decisions >= min_n_for_calibration
    "ece": 0.14,                     // n-weighted mean |predicted - hit_rate|; null below the gate
    "brier": 0.23,                   // mean Brier of the decisions; null below the gate
    "base_rate": 0.55,               // overall realized hit rate
    "mean_confidence": 0.67,         // avg stated confidence; > base_rate ⇒ overconfident
    "bins": [                        // 10 fixed buckets [0,0.1)..[0.9,1.0]
      { "lo": 0.7, "hi": 0.8, "predicted": 0.75, "n": 15, "hit_rate": 0.60 }
      // predicted = MEAN confidence of decisions in the bucket; predicted & hit_rate null when n==0
    ]
  },

  "by_agent": [                      // §3.2 judges / §3.3 personas
    {
      "agent_id": 2, "name": "Momentum juror", "kind": "judge",
      "n": 18, "ece": 0.17, "mean_confidence": 0.74, "hit_rate": 0.55,
      "sharpe": 0.61, "sortino": 0.74   // from the agent's counterfactual book; null if < 2 marks
    }
  ],

  "decisions": [                     // recent scored decisions, newest first (drilldown table)
    {
      "debate_id": 41, "ticker": "TSM", "created_at": "2026-08-01",
      "agent": "Momentum juror", "confidence": 0.86, "decision": "buy",
      "correct": false, "realized_pct": -0.021   // null/null when unresolved
    }
  ]
}
```

## Degradation (matches `history` / performance)

- **DB absent** → `503` with a clear, non-secret `detail` (page shows the calm "not connected" state).
- **No decisions yet** → `200` with `overall.n_decisions: 0`, `bins` all-zero, `metrics` gated null,
  empty `by_agent`/`decisions`.

## Honesty fields — don't drop these

`is_calibratable`/`n_decisions`/`min_n_for_calibration` gate the whole read: with today's handful of
debates the page is *supposed* to show "not yet calibratable" rather than a curve drawn through
noise. `outcome_definition` + `returns_basis` make the scoring rule and its limits explicit. Per-bin
`n` lets the page size the dots and dim thin buckets. All cheap to populate; all the point.

## Frontend done / handoff

- [x] Page + nav entry, reliability diagram (dots sized by n, perfect-calibration diagonal),
      headline stats (ECE, mean-confidence vs hit-rate gap), per-agent table, scored-decisions table
- [x] jury / personas toggle, all loading/empty/degraded states
- [x] Renders now under `NEXT_PUBLIC_CALIB_MOCK=1`
- [ ] **backend:** implement this route → drop the flag → move the types from `lib/calibration.ts`
      into `lib/types.ts` and delete the fixture
