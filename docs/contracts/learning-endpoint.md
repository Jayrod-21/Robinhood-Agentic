# Contract: `GET /api/learning`

Feeds the Learning page (`frontend/src/app/learning/page.tsx`), the surface issue **#145** asks for:
see what the system got right, what broke, and what silently didn't happen. Built and renders today
against a dev fixture (`NEXT_PUBLIC_LEARNING_MOCK=1`, see `frontend/src/lib/learning.ts`). This one
endpoint takes it live. **Read-only** (no writes, no order path, no secrets).

The page is three sections, one per question in #145:
1. **Did the calls turn out right?** accuracy by decision / lens / model family, over time, plus a
   compact calibration read (the full reliability curve stays on `/calibration`).
2. **What didn't happen that should have?** job health (ran / failed / stale / never), juror
   abstentions, unreconciled cycles, unscored judgments.
3. **What changed, and did it help?** accuracy tied to the engine version that produced each verdict.

TypeScript interfaces in `frontend/src/lib/learning.ts` are the source of truth for shapes.

## The one rule this endpoint exists to keep

**An absent measurement must never impersonate a taken one.** This is the defect #145 is about, so it
is a hard requirement of the response, not a UI nicety:

- **Unscored is null, never zero.** A judgment whose scoring window elapsed but was never scored is
  counted in `unscored_absent`, NOT in any accuracy denominator. A bucket with too few scored
  decisions (`n < min_n`) returns `accuracy: null`; the page renders that as "— n=X < min", never as
  0%. Do not send `accuracy: 0` for "we don't know" (22 judgments once read 4.5% correct and the
  number was meaningless; only the denominator showed that).
- **Sample size beside every rate.** Every `AccuracyRow` carries `n` and `correct`. `correct` is only
  meaningful when `accuracy != null`.
- **"Never ran" is distinct from "ran and found nothing".** Job status `never` and `stale` are
  separate from `ok`/`failed`; `unreconciled_cycles` counts cycles that never reconciled (migration
  024 already keeps NULL distinct from false), not cycles that reconciled and found no drift.
- **One regime is not a track record.** Put the caveat in `meta.regime_note` (backend-owned prose);
  the page shows it at the top. Do not omit it just because the window grew.

## Fields the backend owns / computes

- **`scoring.by_decision` / `by_lens` / `by_family`**: group scored judgments by decision
  (`buy`/`sell`/`hold`), by lens (`focus_area`), and by model family (`provider`, available since
  **#142**). `accuracy = correct / n` when `n >= min_n`, else null.
- **`scoring.unscored_absent`**: judgments whose scoring window has closed but that were never scored
  (the dead-cron failure mode). Distinct from **`pending_window`** (window not yet closed, legitimately
  unscored).
- **`scoring.trend`**: overall accuracy per period, each point carrying its own `n`.
- **`calibration`**: a compact digest (`is_calibratable`, `base_rate`, `mean_confidence`, `ece`, `n`).
  `base_rate < mean_confidence` ⇒ overconfident; the page reads the gap out. The full reliability
  diagram is the separate `/api/calibration` contract; keep the two consistent (same scored set).
- **`gaps.jobs`**: one row per scheduled job with `status` in `ok | stale | never | failed`,
  `last_run`, human `schedule`, and a short `detail`. `never` means no record it has ever run;
  `failed` means it ran and errored (including the "failed at line 7 of the env file before its own
  reporting code" case from #144).
- **`gaps.debates_with_abstentions` / `total_abstentions`**: from the abstention/quorum records
  (#141). An outage that produced ten default votes must show here, not as a decisive panel.
- **`engine_versions`**: accuracy per engine version, with `from`/`to` and a `note` on what changed.
  Needs the schema-level engine-version marker #145 asks for; until a verdict is tagged with the
  version that produced it, this array can be empty (the page renders an empty table, not zeros).

## Response

```jsonc
{
  "meta": {
    "generated_at": "2026-08-29T13:00:00Z",
    "window_days": 9,                 // days of scored history, or null
    "regime_note": "Nine days of one mixed tape; one regime is not a track record.",  // or null
    "current_engine_version": "2026.08.3"  // or null
  },
  "scoring": {
    "min_n": 30,                      // gate below which a rate is null
    "total_judgments": 2354,
    "scored": 814,
    "unscored_absent": 340,           // window elapsed, never scored: ABSENT, not wrong
    "pending_window": 1200,           // window not yet closed
    "by_decision": [
      { "key": "hold", "n": 449, "correct": 337, "accuracy": 0.751 },
      { "key": "sell", "n": 321, "correct": 79,  "accuracy": 0.246 },  // anti-predictive, flagged
      { "key": "buy",  "n": 44,  "correct": 37,  "accuracy": 0.841 }
    ],
    "by_lens":   [ { "key": "wasden_framework", "n": 121, "correct": 75, "accuracy": 0.62 },
                   { "key": "macro", "n": 24, "correct": 0, "accuracy": null } ],  // n<min ⇒ null
    "by_family": [ { "key": "anthropic", "n": 410, "correct": 246, "accuracy": 0.601 },
                   { "key": "google",    "n": 404, "correct": 221, "accuracy": 0.547 } ],
    "trend": [ { "period": "Aug 27-29", "n": 316, "accuracy": 0.598 } ]
  },
  "calibration": {
    "is_calibratable": true, "n": 814,
    "base_rate": 0.557, "mean_confidence": 0.712, "ece": 0.155
  },
  "gaps": {
    "jobs": [
      { "name": "score_judgments", "status": "ok", "last_run": "2026-08-29T09:05:00Z",
        "schedule": "hourly", "detail": null },
      { "name": "nightly_marks.sh", "status": "failed", "last_run": "2026-08-27T02:00:00Z",
        "schedule": "nightly 02:00 MT", "detail": "failed at line 7 of the env file" },
      { "name": "weekly_digest", "status": "never", "last_run": null, "schedule": "weekly Mon",
        "detail": "no record it has ever run" }
    ],
    "debates_with_abstentions": 3,
    "total_abstentions": 14,
    "unreconciled_cycles": 2,
    "unscored_judgments": 340
  },
  "engine_versions": [
    { "version": "2026.08.3", "from": "2026-08-27", "to": null, "n": 131, "accuracy": 0.603,
      "note": "paired Claude/Gemini panel (#142), abstentions + quorum (#141)" }
  ]
}
```

## Degradation

- **No scored judgments yet** → `200` with `scoring.scored = 0`, every bucket `accuracy: null`, empty
  `trend`; the page shows the sample-size gates, not zeros.
- **Endpoint not built** → `404`/`503`; the page shows a calm "learning endpoint isn't available yet"
  state (the same degraded pattern the other mock-first pages use).
- **`engine_versions` empty** (no version marker yet) → the section renders an empty table with the
  standing "cannot separate a better engine from an easier tape" caveat, never fabricated rows.

## Don't forget the mock registry

Adding this page introduced `NEXT_PUBLIC_LEARNING_MOCK`, already added to `ANY_MOCK` in
`frontend/src/lib/dataTrust.ts` so the data-trust strip keeps warning while the Learning page is mock.
When the endpoint lands and the flag is dropped, leave `ANY_MOCK` intact (the OR just goes false).

## Frontend done / handoff

- [x] Route `/learning` + nav entry (Learning, GraduationCap icon)
- [x] Section 1 (accuracy by decision / lens / family, anti-predictive flag, trend chart, link to the
      full `/calibration` curve), Section 2 (job-health table, abstentions / unreconciled / unscored),
      Section 3 (accuracy by engine version)
- [x] Honesty rendering: null accuracy shown as "— n=X < min", `unscored_absent` shown as absent,
      regime caveat banner, sample size on every rate
- [x] Renders under `NEXT_PUBLIC_LEARNING_MOCK=1`
- [ ] **backend:** implement `GET /api/learning` aggregating scored judgments + job health + engine
      versions; drop the flag; move the types from `lib/learning.ts` into `lib/types.ts`; delete the
      fixture. The engine-version marker (tag each verdict with the version that produced it) is the
      one schema change this needs.
