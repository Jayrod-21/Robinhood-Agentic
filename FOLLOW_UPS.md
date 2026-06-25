# Follow-ups — Agentic Dashboard

From the `/fixpass` cycle (2026-06-16). Cycle verdict: **PASS — shipped.** Full paper trail in
`docs/fixpass/` (AGGREGATE.md, REVIEW_*.md, FIX_REPORT.md, REVIEW_FIXES.md). These are the items the
fix-pass deliberately DEFERRED, plus low-priority NITs from the re-review. None block shipping.

- **SF-3 — deploy-time model-id check** (low priority, recommended). A typo'd `JURY_MODEL` /
  `SYNTH_MODEL` alias would surface as confident-looking unanimous HOLDs rather than a clear error.
  Add a startup probe (or a `/api/health` field) that validates the configured model ids. Deferred
  because a real probe needs network at boot.
- **`list_records` resilience** — narrowing the broad `except` was deferred to avoid weakening the
  praised "one bad record doesn't break the list" behavior. Revisit if record corruption becomes a
  real concern.
- **Frontend `(event: any)` → discriminated union** — typing each SSE event payload as a tagged union
  would tighten the streaming pages, but it's a scope-expanding refactor; shipped as-is.
- **Debate vote dedupe** — defensive-only; the backend never emits a duplicate juror event today.
- **Re-review NITs (4)** — minor; see `docs/fixpass/REVIEW_FIXES.md` § New findings.
