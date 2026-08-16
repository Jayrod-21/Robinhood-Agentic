# Contract: `GET /api/data-trust`

Feeds the Data-Trust strip (`frontend/src/components/data-trust.tsx`), the thin always-visible bar
the Shell renders at the top of every page. It is built and renders today against a dev fixture
(`NEXT_PUBLIC_TRUST_MOCK=1`, see `frontend/src/lib/dataTrust.ts`). This one endpoint takes it live.
**Read-only** (no writes, no order path, no secrets).

The strip is one small fetch on every page, so this endpoint must stay cheap: a summary, never the
whole portfolio. Every field already exists in the backend; this route just bundles the account
snapshot's freshness/pricing signals with `/api/health`'s posture flags. TypeScript interfaces in
`frontend/src/lib/dataTrust.ts` are the source of truth for shapes.

## Sources (all already in the system)

- **Freshness** from the account snapshot: `snapshot_generated_at = snapshot.generated_at`,
  `snapshot_stale` = the same staleness rule the account view already applies.
- **Pricing** from the live FMP marks the account endpoint computes: `price_source` ("FMP"),
  `prices_degraded = account.stale_prices`, `positions_total` = held names, `positions_priced` =
  those that priced live (`PositionView.priced == true`).
- **Returns basis**: `price_only` until dividends/total-return are wired (matches the Performance
  page's `returns_basis`); a standing honesty caveat the strip surfaces.
- **Posture** from `/api/health`: `debate_live = health.debate_ready` (a key is present, live debates
  cost tokens) and `auth_enforced = health.auth_enforced`.

Prefer reading the snapshot once here rather than proxying `/api/account`; the strip must not pay for
a full portfolio payload on pages (Scan, Pipeline, Debate) that never show one.

## Response

```jsonc
{
  "snapshot_generated_at": "2026-07-27T15:30:00Z",
  "snapshot_stale": true,
  "price_source": "FMP",
  "prices_degraded": true,      // account.stale_prices: at least one held name unpriced
  "positions_total": 8,
  "positions_priced": 6,
  "returns_basis": "price_only", // "price_only" | "total_return"
  "debate_live": false,          // health.debate_ready
  "auth_enforced": true          // health.auth_enforced
}
```

## Degradation

- **Snapshot unavailable** → still `200`; set `snapshot_generated_at: null`, `snapshot_stale: true`,
  `positions_total: 0`, `positions_priced: 0`. The strip must be able to render a truthful "we have
  no fresh data" state, which is different from the endpoint being unreachable.
- **Endpoint unreachable / not yet implemented** → the strip shows a muted "data status unavailable"
  and, if any page is mocked, still shows the MOCK chip. It never invents a green state. So a `501`
  or a missing route degrades cleanly; you do not need to ship a stub to avoid a scary banner.

## Why the strip owns the mock signal, not this endpoint

Whether a page is showing a fixture is a *frontend* fact (the `NEXT_PUBLIC_*_MOCK` build flags), so
the strip computes `ANY_MOCK` itself and this endpoint never reports it. Keep the flag list in
`lib/dataTrust.ts` (`ANY_MOCK`) in sync as new mock-gated pages are added, or the strip will quietly
stop warning that a new page is mock.

## Frontend done / handoff

- [x] Strip component + Shell renders it on every non-public page (sticky, full-bleed, theme-matched)
- [x] Freshness / source+coverage / price-only / auth / debates chips, plus a right-aligned MOCK chip
- [x] Fails honest: "data status unavailable" on error, never a green it hasn't earned
- [x] Renders now under `NEXT_PUBLIC_TRUST_MOCK=1`
- [ ] **backend:** implement this route → drop the flag → move the type from `lib/dataTrust.ts` into
      `lib/types.ts` and delete the fixture (keep `ANY_MOCK` in `lib/dataTrust.ts`, it is frontend-only)
