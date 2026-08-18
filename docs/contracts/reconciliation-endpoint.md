# Contract: `GET /api/reconciliation` (issues #22 / #2)

The Reconciliation page (`frontend/src/app/reconciliation/page.tsx`) is built and renders today
against a dev fixture (`NEXT_PUBLIC_RECON_MOCK=1`, see `frontend/src/lib/reconciliation.ts`). This
one endpoint takes it live. **Read-only** (no writes, no order path, no secrets).

It answers issue #22 directly: the broker does not hold what the repo says it holds, and nothing
reconciles them. This endpoint is the "reconciliation step, loud not silent" that #22 part 2 asks
for, per `SENIOR_ENGINEER_BAR.md` §7.2 ("treat local state as a cache of broker truth; periodic
reconciliation diff, alert/halt on mismatch"). The same diff the backend needs for the cycle's
alert/halt is what this returns. TypeScript interfaces in `frontend/src/lib/reconciliation.ts` are
the source of truth for shapes.

## Sources

- **Documented slate** from `docs/SLATE.md`: the target weights table (TSM 22 / VST 15 / NVDA 13 /
  V 12 / CVX 11 / GEV 9 / QCOM 6 / PLTR 2 / CASH 10, as of 2026-06-03). Percent of account value.
- **Broker truth** from the existing `/api/account` snapshot (`AccountView.positions`), using the
  account-value weight basis (`weight_account_pct`), plus cash and total value.
- **Universe** from `src/universe.py`, to flag off-universe holdings (e.g. SVRA).
- **Discipline rules** from the charter and `docs/SLATE.md` sizing section.

## Diff logic

For every symbol in (slate ∪ live):

- in slate and held, |drift| <= tolerance → `match`
- in slate and held, |drift| > tolerance → `drifted` (suggest ~1.5 points; your call, state it)
- in slate, not held → `missing` (an exit with no record: GEV, PLTR)
- held, not in slate → `unexpected` (an entry with no record: MU, SVRA)

`drift_pct = live_weight_pct - target_weight_pct` (absolute points), and `drift_rel_pct =
drift_pct / target_weight_pct * 100` (the same drift relative to the target, null when target is 0 or
absent). Status is driven by the ABSOLUTE threshold, returned as `meta.drift_tolerance_pct` so the UI
states the rule instead of hardcoding it; the relative figure is display-only, because 1.5 points is
7% of a 22% target but 50% of a 3% one. Cash is reported in `meta` (target vs live), not as a position
row. `in_sync` is true only when missing = unexpected = 0 and nothing has drifted.

## Discipline checks

Evaluate the charter/slate rules and return each as a row (rule, source line, ok/breach, severity,
detail). At minimum: max ~25% per name (charter:67), cash 10-20% band (charter:66), off-factor floor
V + CVX >= 20% (SLATE.md:40), hard stop -20% per name (SLATE.md:37). Severity: `alert` for a hard
rule breached (a name past its stop), `warn` for a soft breach (cash band, floor), `info` for a pass.

## Response

```jsonc
{
  "meta": {
    "slate_source": "docs/SLATE.md",
    "slate_dated": "2026-06-03",
    "snapshot_generated_at": "2026-07-27T15:30:00Z",
    "snapshot_stale": true,             // reuse the account snapshot's staleness signal
    "account_value": 239.79,
    "documented_book_value": 100.00,    // what the slate assumed; a gap means unaccounted deposits
    "target_cash_pct": 10.0,
    "live_cash_pct": 38.6,
    "drift_tolerance_pct": 1.5,         // absolute-points threshold for match vs drifted; UI states it
    "in_sync": false
  },
  "positions": [
    {
      "symbol": "GEV",
      "target_weight_pct": 9.0,         // null when unexpected
      "live_weight_pct": null,          // null when missing
      "drift_pct": null,                // live - target (absolute pts); null if either side absent
      "drift_rel_pct": null,            // drift relative to target (%); null if target 0/absent
      "status": "missing",              // match | drifted | missing | unexpected
      "market_value": null,
      "unrealized_pl_pct": null,
      "in_universe": true,
      "note": "no exit recorded in journal or logs/trades"
    }
  ],
  "checks": [
    { "rule": "Hard stop -20% per name", "source": "SLATE.md:37", "status": "breach",
      "severity": "alert", "detail": "QCOM -27.0% has breached; MU -18.2% is near." }
  ],
  "summary": {
    "matched": 1, "drifted": 5, "missing": 2, "unexpected": 2,
    "checks_total": 4, "checks_failing": 3
  }
}
```

## Degradation (matches the other read endpoints)

- **Snapshot or slate unavailable** → `503` with a clear, non-secret `detail` (the page shows a calm
  "nothing to reconcile yet" state, not an error).
- **In sync** → `200` with `in_sync: true`, empty problem rows; the page shows a green "broker holds
  what the slate says" banner.

## Honesty fields, please keep them

`snapshot_stale` (reconciling against an old snapshot is a caveat the operator must see),
`documented_book_value` vs `account_value` (surfaces the unrecorded deposits), and `in_universe`
(SVRA is off-universe and needs a thesis or an exit). These are the difference between a diff that
informs and one that quietly misleads.

## Frontend done / handoff

- [x] Page + nav entry (Reconcile), in-sync/drift banner, stale-snapshot and deposit notes, summary
      stats, slate-vs-broker table (problems sorted first), discipline-checks list
- [x] Renders now under `NEXT_PUBLIC_RECON_MOCK=1`
- [ ] **backend:** implement this route (also feeds the cycle's alert/halt for #22 part 2) → drop the
      flag → move the types from `lib/reconciliation.ts` into `lib/types.ts` and delete the fixture
