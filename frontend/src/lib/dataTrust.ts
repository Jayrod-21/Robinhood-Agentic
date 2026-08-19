// Types plus a dev-only fixture for the Data-Trust strip: the thin always-visible bar (in the Shell)
// that tells you, on every page, how much to trust what you are looking at. Freshness of the account
// snapshot, the price source and how many positions actually priced, whether returns are price-only,
// and the two posture facts that change how much the numbers mean (auth enforced, live debates on).
//
// Mirrors the PROPOSED `GET /api/data-trust` contract in docs/contracts/data-trust-endpoint.md. The
// backend already has every field: the account snapshot (generated_at, source, stale_prices, per-
// position `priced`) and /api/health (debate_ready, auth_enforced). This route just bundles them so
// the strip is one cheap fetch that does NOT pull the whole portfolio on pages that don't need it.
//
// Not in lib/types.ts on purpose (same reason as perf/calibration/reconciliation/position): that
// file mirrors shapes a real backend returns today, and this endpoint does not exist yet.

import type { ReturnsBasis } from "@/lib/perf";

export interface DataTrustResponse {
  // ── Freshness (from the account snapshot) ──
  snapshot_generated_at: string | null;
  /** Snapshot older than the freshness window; the operator should refresh before acting. */
  snapshot_stale: boolean;
  // ── Pricing (from the live FMP marks) ──
  price_source: string;
  /** At least one held symbol could not be priced live (account.stale_prices). */
  prices_degraded: boolean;
  positions_total: number;
  positions_priced: number;
  /** price_only is a standing honesty caveat: dividends/total-return are not in these numbers. */
  returns_basis: ReturnsBasis;
  // ── Posture (from /api/health): changes how much the numbers above can be trusted ──
  /** Live debates are on (cost API tokens) vs the debate pages being history-only. */
  debate_live: boolean;
  /** Protected routes require an operator session. False is the legitimate pre-auth posture, but it
   *  is also what a mislaid backend/.env looks like, so it is shown, never inferred. */
  auth_enforced: boolean;
}

// A single boolean the strip can trust: true when ANY page in the app is showing a fixture instead
// of real data. Read straight from the build-time NEXT_PUBLIC_*_MOCK flags (Next inlines them), so
// the strip can say "some views are mock" without asking the backend. Keep this list in sync as new
// mock-gated pages are added.
export const ANY_MOCK =
  process.env.NEXT_PUBLIC_PERF_MOCK === "1" ||
  process.env.NEXT_PUBLIC_CALIB_MOCK === "1" ||
  process.env.NEXT_PUBLIC_RECON_MOCK === "1" ||
  process.env.NEXT_PUBLIC_POSITION_MOCK === "1" ||
  process.env.NEXT_PUBLIC_MARKET_MOCK === "1" ||
  process.env.NEXT_PUBLIC_TESTLAB_MOCK === "1" ||
  process.env.NEXT_PUBLIC_TRUST_MOCK === "1";

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_TRUST_MOCK=1); the default hits the real endpoint. The numbers match the
// reconciliation/position fixtures: a stale (2026-07-27) snapshot, 8 held names with 2 that couldn't
// be priced, price-only returns, live debates off, and (for the demo) auth enforced.
export const TRUST_MOCK = process.env.NEXT_PUBLIC_TRUST_MOCK === "1";

export const MOCK_DATA_TRUST: DataTrustResponse = {
  snapshot_generated_at: "2026-07-27T15:30:00Z",
  snapshot_stale: true,
  price_source: "FMP",
  prices_degraded: true,
  positions_total: 8,
  positions_priced: 6,
  returns_basis: "price_only",
  debate_live: false,
  auth_enforced: true,
};
