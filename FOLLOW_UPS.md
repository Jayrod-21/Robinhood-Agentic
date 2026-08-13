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

## From the Phase A data-foundation fix-pass (2026-07-29)

Full paper trail: `docs/fixpass/REVIEW_phaseA_loaders.md`, `REVIEW_phaseA_semantics.md`,
`FIX_REPORT_phaseA.md`. These are the items that fix-pass deliberately deferred; the schema
guards that make each one safe-to-defer are already in force.

- **Full-universe dividend fetch** (B-S2 data half) — run
  `bin/db_corporate_actions.sh fetch --candidates all` (~14,600 rate-limited yfinance calls,
  hours). Until it lands, every computed return is `price_only`, and
  `evaluation_runs.return_basis` refuses to store it as anything else. Run `adjust` afterwards.
- **Official-close source** (B-S4) — the stored daily close is the 15:59 ET bar; the closing
  auction cannot be recovered from minute aggregates. Cheapest correct fix: Polygon's daily
  aggregates endpoint (one call per symbol-range). When it lands, tighten the thresholds in
  `db/verify_daily_series.py` (close ≤ ~1 bps, return-sd ≤ ~1 bps/day, volume band → ~1.0).
- **December-2024 hole** — 15 sessions of corrupt source members; re-copy from the source drive
  and re-run the daily loader (resumes by hash). `expected_sessions`/`coverage_ratio` (007) make
  the hole undeclarable in any metric row meanwhile.
- **`adj_close` → `split_adj_close` rename** (N-S1) — do as its own one-line migration +
  mechanical rename before any consumer of the column lands.
- **Securities metadata** (S-S7) — name/exchange/security_type/sector are NULL; needs a
  reference-metadata feed (FMP profile is per-symbol and not free-tier feasible). Until then a
  universe query cannot exclude warrants/rights/preferreds except by symbol form.
- **Delisted identities' corporate actions** — unfetchable from yfinance (it resolves a symbol
  to its current holder). Needs an identity-aware feed (FMP Premium / Polygon reference); their
  series stay factor-1 and are excluded from fetch with a counted warning.
- **Ticker recycles are now evidence-classified, not threshold-guessed** (B-N2, round 2) — the
  180d/120d threshold splices are superseded by `load_delistings.py audit`: every internal hole
  of ≥10 missed covered sessions gets a disposition in `price_gap_audit` (ratio evidence, then
  provider-history evidence), `splice --from-audit` splices confirmed/unresolvable identity
  breaks, and `verify_daily_series.py` check 7 FAILS while any out-of-band hole is unclassified.
  The tripwire is universe-wide; the 20-symbol alignment check never was one (recycling happens
  in delisted small caps, zero overlap with mega-cap reference names). Two counted residuals,
  stored rather than assumed empty: in-band holes (`halt_consistent` — a similar-priced recycle
  is invisible to ratio evidence) and sub-floor out-of-band holes (logged by every audit run).
