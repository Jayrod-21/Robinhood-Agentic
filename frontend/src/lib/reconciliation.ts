// Types plus a dev-only fixture for the Reconciliation page (issues #22 / #2).
//
// Mirrors the PROPOSED `GET /api/reconciliation` contract in
// docs/contracts/reconciliation-endpoint.md. The backend diffs the documented slate
// (docs/SLATE.md target weights) against broker truth (the /api/account snapshot) and applies the
// charter discipline rules. This is exactly the "reconciliation step, loud not silent" that issue
// #22 asks for, per SENIOR_ENGINEER_BAR §7.2 ("treat local state as a cache of broker truth").
//
// Not in lib/types.ts on purpose (same reason as perf.ts / calibration.ts): that file mirrors
// shapes a real backend returns today, and this endpoint does not exist yet.

// match     : held and documented, weight within tolerance of target
// drifted   : held and documented, weight materially off target
// missing   : documented in the slate but NOT held (an exit with no record)
// unexpected: held but NOT in the slate (an entry with no record)
export type PositionStatus = "match" | "drifted" | "missing" | "unexpected";

export interface ReconPosition {
  symbol: string;
  /** Target weight from the slate (percent of account value); null when unexpected. */
  target_weight_pct: number | null;
  /** Live weight from the broker snapshot (account-value basis); null when missing. */
  live_weight_pct: number | null;
  /** live - target, in percentage points; null when either side is absent. */
  drift_pct: number | null;
  status: PositionStatus;
  market_value: number | null;
  unrealized_pl_pct: number | null;
  /** False when the symbol is not in src/universe.py (e.g. SVRA). */
  in_universe: boolean;
  /** Why this row is flagged: "no exit recorded", "off-universe", stop breach, and so on. */
  note: string | null;
}

export type CheckStatus = "ok" | "breach";
// info : informational, passing
// warn : a soft breach the charter wants corrected (cash band, off-factor floor)
// alert: a hard-rule breach (a name past its stop) that demands a decision now
export type CheckSeverity = "info" | "warn" | "alert";

export interface DisciplineCheck {
  rule: string;
  /** Where the rule is written, so a breach is auditable: "charter:66", "SLATE.md:37". */
  source: string;
  status: CheckStatus;
  severity: CheckSeverity;
  detail: string;
}

export interface ReconMeta {
  slate_source: string;
  slate_dated: string;
  snapshot_generated_at: string | null;
  /** Reconciling against a stale snapshot is itself a caveat, so it is surfaced, not hidden. */
  snapshot_stale: boolean;
  account_value: number | null;
  /** What the slate assumed the book was worth (the $100 bootstrap); a gap means deposits the
   *  slate never accounted for. */
  documented_book_value: number | null;
  target_cash_pct: number | null;
  live_cash_pct: number | null;
  /** True only when zero positions are missing/unexpected AND nothing has drifted materially. */
  in_sync: boolean;
}

export interface ReconSummary {
  matched: number;
  drifted: number;
  missing: number;
  unexpected: number;
  checks_total: number;
  checks_failing: number;
}

export interface ReconciliationResponse {
  meta: ReconMeta;
  positions: ReconPosition[];
  checks: DisciplineCheck[];
  summary: ReconSummary;
}

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_RECON_MOCK=1), page stamps a MOCK badge, real fetch otherwise. The
// numbers are the ACTUAL drift documented in issue #22: slate of 2026-06-03 vs the 2026-07-27
// snapshot. GEV and PLTR vanished with no recorded exit; MU and SVRA appeared with no recorded
// entry (SVRA is off-universe); the book is 38.6% cash (under-deployed) and QCOM is past its stop.
export const RECON_MOCK = process.env.NEXT_PUBLIC_RECON_MOCK === "1";

const ACCOUNT_VALUE = 239.79;
const mv = (weightPct: number) => Number(((weightPct / 100) * ACCOUNT_VALUE).toFixed(2));

const POSITIONS: ReconPosition[] = [
  { symbol: "TSM", target_weight_pct: 22, live_weight_pct: 15.9, drift_pct: -6.1, status: "drifted", market_value: mv(15.9), unrealized_pl_pct: 2.1, in_universe: true, note: null },
  { symbol: "VST", target_weight_pct: 15, live_weight_pct: 9.5, drift_pct: -5.5, status: "drifted", market_value: mv(9.5), unrealized_pl_pct: -3.5, in_universe: true, note: null },
  { symbol: "NVDA", target_weight_pct: 13, live_weight_pct: 11.0, drift_pct: -2.0, status: "drifted", market_value: mv(11.0), unrealized_pl_pct: 4.2, in_universe: true, note: null },
  { symbol: "V", target_weight_pct: 12, live_weight_pct: 5.6, drift_pct: -6.4, status: "drifted", market_value: mv(5.6), unrealized_pl_pct: -1.8, in_universe: true, note: "off-factor leg well under target" },
  { symbol: "CVX", target_weight_pct: 11, live_weight_pct: 4.8, drift_pct: -6.2, status: "drifted", market_value: mv(4.8), unrealized_pl_pct: 0.9, in_universe: true, note: "off-factor leg well under target" },
  { symbol: "QCOM", target_weight_pct: 6, live_weight_pct: 5.2, drift_pct: -0.8, status: "match", market_value: mv(5.2), unrealized_pl_pct: -27.0, in_universe: true, note: "stop breach: -27% past the -20% line" },
  { symbol: "GEV", target_weight_pct: 9, live_weight_pct: null, drift_pct: null, status: "missing", market_value: null, unrealized_pl_pct: null, in_universe: true, note: "no exit recorded in journal or logs/trades" },
  { symbol: "PLTR", target_weight_pct: 2, live_weight_pct: null, drift_pct: null, status: "missing", market_value: null, unrealized_pl_pct: null, in_universe: true, note: "rental slot never entered; no record" },
  { symbol: "MU", target_weight_pct: null, live_weight_pct: 6.9, drift_pct: null, status: "unexpected", market_value: mv(6.9), unrealized_pl_pct: -18.2, in_universe: true, note: "no entry recorded; -18.2% near its stop" },
  { symbol: "SVRA", target_weight_pct: null, live_weight_pct: 2.5, drift_pct: null, status: "unexpected", market_value: mv(2.5), unrealized_pl_pct: -8.0, in_universe: false, note: "off-universe: not in src/universe.py; needs a thesis or an exit" },
];

const CHECKS: DisciplineCheck[] = [
  { rule: "Max ~25% per name", source: "charter:67", status: "ok", severity: "info", detail: "OK. TSM is the largest at 15.9%." },
  { rule: "Cash 10-20% of account", source: "charter:66", status: "breach", severity: "warn", detail: "Cash is 38.6%. The book is under-deployed; the band is 10 to 20%." },
  { rule: "Off-factor floor: V + CVX >= 20%", source: "SLATE.md:40", status: "breach", severity: "warn", detail: "V + CVX = 10.4% of account, roughly half the floor." },
  { rule: "Hard stop -20% per name", source: "SLATE.md:37", status: "breach", severity: "alert", detail: "QCOM -27.0% has breached; MU -18.2% is near. Re-underwrite or exit, do not average." },
];

export const MOCK_RECONCILIATION: ReconciliationResponse = {
  meta: {
    slate_source: "docs/SLATE.md",
    slate_dated: "2026-06-03",
    snapshot_generated_at: "2026-07-27T15:30:00Z",
    snapshot_stale: true,
    account_value: ACCOUNT_VALUE,
    documented_book_value: 100,
    target_cash_pct: 10,
    live_cash_pct: 38.6,
    in_sync: false,
  },
  positions: POSITIONS,
  checks: CHECKS,
  summary: {
    matched: POSITIONS.filter((p) => p.status === "match").length,
    drifted: POSITIONS.filter((p) => p.status === "drifted").length,
    missing: POSITIONS.filter((p) => p.status === "missing").length,
    unexpected: POSITIONS.filter((p) => p.status === "unexpected").length,
    checks_total: CHECKS.length,
    checks_failing: CHECKS.filter((c) => c.status === "breach").length,
  },
};
