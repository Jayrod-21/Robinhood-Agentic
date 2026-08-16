# Contract: `GET /api/position/{symbol}`

The Per-Position detail page (`frontend/src/app/position/[symbol]/page.tsx`) is built and renders
today against a dev fixture (`NEXT_PUBLIC_POSITION_MOCK=1`, see `frontend/src/lib/position.ts`).
This one endpoint takes it live. **Read-only** (no writes, no order path, no secrets).

It is the drill-down behind every Portfolio row: click a symbol, get everything the system already
knows about that one name in a single place. All the data exists somewhere in the backend already;
this route just bundles it so the page is one fetch and the FMP price history stays server-side.
TypeScript interfaces in `frontend/src/lib/position.ts` are the source of truth for shapes.

## Sources (all already in the system)

- **Live holding** from the existing `/api/account` snapshot: the matching `PositionView` (quantity,
  avg cost, current price, market value, unrealized P&L, `weight_account_pct`). `live` is `null` when
  the name is in the slate but not held (a "missing" position, e.g. GEV/PLTR).
- **Slate target + role** from `docs/SLATE.md`: `target_weight_pct`, the "Role" column, and the "Why
  that size" column (`size_rationale`). `drift_pct = weight_account_pct - target_weight_pct`.
- **Thesis** from `THESES.md` (per the slate). `null` when nothing is on record (an unrecorded
  holding such as MU/SVRA); the page turns that absence into a loud "needs a case or an exit".
- **Discipline math** from `docs/SLATE.md` §Sizing: -20% hard stop, 1.3x-target trim line.
- **Price history** from FMP (server-side key), the daily close series. ~90 trading days is plenty.
- **Last debate** from the debate records (same store `/api/debate/{id}` reads): the most recent
  debate whose ticker == symbol, compressed to verdict + both cases + jury tally, with its `id` so
  the page can link to the full `/debate/[id]` record.

## `held` vs `live`

`meta.held` is false when the slate documents the name but the broker does not hold it. The page
still renders (thesis, target, last debate, price history); `live` and `stop` come back `null` and
their cards degrade to a "not held" note. This is deliberate: a documented-but-missing name is
exactly the case an operator needs to see, not a 404.

## Derived fields the backend owns

- `stop.distance_to_stop_pct = unrealized_pl_pct - (-20)` (positive = cushion; <= 0 = at/past stop).
- `stop.breached = unrealized_pl_pct <= -20`.
- `stop.trim_line_weight_pct = 1.3 * target_weight_pct`; `above_trim_line = weight_account_pct >` that.
- `thesis.status`: `broken` if the stop is breached OR the name is held with no thesis on record;
  `watch` if within ~5 points of the stop or drifted >= 5 points; else `intact`. State your rule.

## Response

```jsonc
{
  "meta": {
    "symbol": "QCOM",
    "name": "Qualcomm Inc",
    "sector": "Technology",
    "snapshot_generated_at": "2026-07-27T15:30:00Z",
    "snapshot_stale": true,           // reuse the account snapshot's staleness signal
    "price_source": "FMP",
    "price_history_from": "2026-03-23",
    "held": true
  },
  "live": {                           // null when not held
    "quantity": 0.0739, "average_buy_price": 227.60, "current_price": 168.90,
    "cost_basis": 16.82, "market_value": 12.47, "unrealized_pl": -4.35,
    "unrealized_pl_pct": -27.0, "weight_account_pct": 5.2, "weight_pct": null, "priced": true
  },
  "slate": {
    "in_slate": true, "in_universe": true, "target_weight_pct": 6.0,
    "role": "Cheap edge satellite", "size_rationale": "~14x Snapdragon + AI200/AI250 inference; asymmetric, kept small.",
    "drift_pct": -0.8
  },
  "stop": {                           // null when not held
    "hard_stop_pct": -20, "distance_to_stop_pct": -7.0, "breached": true,
    "trim_line_weight_pct": 7.8, "above_trim_line": false
  },
  "thesis": {
    "status": "broken", "summary": "A cheap, asymmetric edge-inference call kept deliberately small...",
    "updated_at": "2026-06-03T18:00:00Z"
  },
  "price_history": [ { "date": "2026-03-23", "close": 231.10 }, { "date": "2026-03-24", "close": 229.55 } ],
  "debate": {
    "id": "dbt_qcom_2026-06-03", "created_at": "2026-06-03T18:00:00Z",
    "question": "Does QCOM earn its 6% slot in the barbell?",
    "decision": "ESCALATED", "escalated": true,
    "bull_case": "…", "bear_case": "…",
    "jury_counts": { "BUY": 4, "SELL": 1, "HOLD": 5 }, "jury_total": 10
  }
}
```

## Degradation (matches the other read endpoints)

- **Symbol not held, not in slate, no debate** → `404` with a non-secret `detail` (the page shows a
  calm "nothing on record for X" state, not a red error).
- **Snapshot or price source unavailable** → `503` with a clear `detail` (page shows a "fills in
  once readable" state).
- **No price history from FMP** → return `price_history: []`; the page shows a "no history" note and
  still renders everything else. Never fabricate a series to fill the chart.

## Honesty fields, please keep them

`snapshot_stale`, `price_source` (a fallback or stale source must never be silent), `held`,
`in_slate`/`in_universe` (an unrecorded or off-universe holding is a finding, not a footnote), and a
`null` `thesis.summary` for a name with no case on record. These are the difference between a
drill-down that informs and one that quietly implies everything is fine.

## Frontend done / handoff

- [x] Route `position/[symbol]` + Portfolio rows link into it (symbol cell → `/position/{symbol}`)
- [x] Stop/thesis alarm banner, live stat cards, FMP price chart (avg-cost reference line), thesis +
      last-debate cards (links to `/debate/[id]`), discipline card (stop + trim), not-held/unrecorded states
- [x] Renders now under `NEXT_PUBLIC_POSITION_MOCK=1`
- [ ] **backend:** implement this route → drop the flag → move the types from `lib/position.ts` into
      `lib/types.ts` and delete the fixture
