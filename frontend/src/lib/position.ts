// Types plus a dev-only fixture for the Per-Position detail page (drill-down from Portfolio).
//
// Mirrors the PROPOSED `GET /api/position/{symbol}` contract in
// docs/contracts/position-endpoint.md. The backend bundles what is already scattered across the
// system for one name: the live holding (the /api/account snapshot row), its slate target and role
// (docs/SLATE.md), its written thesis (THESES.md), stop/trim discipline math, an FMP price history,
// and the most recent debate that put it there (the debate records). One call so the page is one
// fetch, and the price history stays server-side behind the FMP key (never a browser->FMP call).
//
// Not in lib/types.ts on purpose (same reason as perf.ts / calibration.ts / reconciliation.ts):
// that file mirrors shapes a real backend returns today, and this endpoint does not exist yet.

import type { Decision } from "@/lib/types";

export interface PricePoint {
  /** Trading day, ISO date (no time). */
  date: string;
  close: number;
}

/** The live holding, straight from the /api/account snapshot's matching PositionView. Null when the
 *  name is documented in the slate but not actually held (a "missing" position, still worth a page). */
export interface PositionLive {
  quantity: number;
  average_buy_price: number;
  current_price: number | null;
  cost_basis: number;
  market_value: number | null;
  unrealized_pl: number | null;
  unrealized_pl_pct: number | null;
  /** Share of account value (equity + cash): the basis the ~25%/name cap is written against. */
  weight_account_pct: number | null;
  /** Share of invested equity only. */
  weight_pct: number | null;
  priced: boolean;
}

export interface PositionSlate {
  /** In docs/SLATE.md target table. False for an unrecorded holding (MU, SVRA). */
  in_slate: boolean;
  /** In src/universe.py. False for an off-universe name (SVRA). */
  in_universe: boolean;
  target_weight_pct: number | null;
  /** The slate's "Role" column, e.g. "Compute anchor". */
  role: string | null;
  /** The slate's "Why that size" column. */
  size_rationale: string | null;
  /** live weight (account basis) - target weight, in percentage points; null if either absent. */
  drift_pct: number | null;
}

/** Discipline math for this name: the -20% hard stop and the 1.3x-target trim line (SLATE.md §Sizing). */
export interface StopState {
  hard_stop_pct: number;
  /** unrealized_pl_pct - hard_stop_pct. Positive = cushion left; <= 0 = at or past the stop. */
  distance_to_stop_pct: number | null;
  breached: boolean;
  /** 1.3 x target weight (account basis); the level past which the slate says trim a winner. */
  trim_line_weight_pct: number | null;
  above_trim_line: boolean;
}

// intact : holding as underwritten, no discipline line crossed
// watch  : within ~5 points of the stop, or drifted materially, or a soft breach nearby
// broken : stop breached, or held with no thesis on record (unrecorded entry)
export type ThesisStatus = "intact" | "watch" | "broken";

export interface PositionThesis {
  status: ThesisStatus;
  /** The name's entry in THESES.md; null when nothing is on record (an unrecorded holding). */
  summary: string | null;
  updated_at: string | null;
}

/** A compressed view of the most recent debate for this ticker (the full record lives at
 *  /debate/[id]). Enough to show the verdict and the two cases without a second fetch. */
export interface LinkedDebate {
  id: string;
  created_at: string | null;
  question: string | null;
  decision: Decision | null;
  escalated: boolean;
  bull_case: string | null;
  bear_case: string | null;
  /** BUY/SELL/HOLD tallies from the jury, and the total voter count. */
  jury_counts: Record<string, number> | null;
  jury_total: number | null;
}

export interface PositionDetailMeta {
  symbol: string;
  name: string | null;
  sector: string | null;
  snapshot_generated_at: string | null;
  snapshot_stale: boolean;
  /** "FMP" once live; surfaced so a stale or fallback source is never silent. */
  price_source: string;
  price_history_from: string | null;
  /** False when the slate documents this name but the broker does not hold it. The page still
   *  renders (thesis, target, last debate); the live/stop cards degrade to a "not held" note. */
  held: boolean;
}

export interface PositionDetailResponse {
  meta: PositionDetailMeta;
  live: PositionLive | null;
  slate: PositionSlate;
  stop: StopState | null;
  thesis: PositionThesis;
  price_history: PricePoint[];
  debate: LinkedDebate | null;
}

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_POSITION_MOCK=1); the page stamps a MOCK badge; a real fetch happens
// otherwise. Live weights, P&L, and universe flags are kept CONSISTENT with the reconciliation
// fixture (lib/reconciliation.ts) so the same name reads the same across pages: TSM healthy anchor,
// QCOM past its stop, GEV documented-but-not-held.
export const POSITION_MOCK = process.env.NEXT_PUBLIC_POSITION_MOCK === "1";

const ACCOUNT_VALUE = 239.79;
const HARD_STOP_PCT = -20;
const SNAPSHOT_AT = "2026-07-27T15:30:00Z";
// A fixed anchor for the mock price series so server and client render identical dates (no
// Date.now() in the fixture, which would hydrate-mismatch and drift the chart between renders).
const HISTORY_END = "2026-07-27";

// docs/SLATE.md, the 2026-06-03 target table. Role + rationale are the slate's own columns; the
// thesis line is a one-sentence compression of THESES.md for the drill-down.
interface SlateRow {
  target: number;
  role: string;
  rationale: string;
  thesis: string;
  sector: string;
  name: string;
}

const SLATE: Record<string, SlateRow> = {
  TSM: { target: 22, role: "Compute anchor", rationale: "Lowest-variance way to be long all silicon (builds NVDA's chips too); top weight = lowest blowup risk.", thesis: "The safest way to own the AI-buildout: TSMC makes everyone's chips, so it wins regardless of which designer leads. Top weight because it is the lowest-variance leg, not the highest-conviction one.", sector: "Technology", name: "Taiwan Semiconductor" },
  VST: { target: 15, role: "Power leg + floor", rationale: "FCFy 7.2%, ~18x, Meta 2,600 MW PPAs not in guidance = free option; safest AI-power beta, sized above GEV.", thesis: "AI needs power before it needs more chips. VST is the cash-generative, cheap way to own that, with unguided Meta PPAs as a free option. Sized above GEV as the safer end of the power barbell.", sector: "Utilities", name: "Vistra Corp" },
  NVDA: { target: 13, role: "Convexity engine", rationale: "Wins cloud AND edge (RTX Spark), fwd P/E ~22; sized below TSM = most backlash-exposed.", thesis: "The convexity leg: biggest upside if the capex cycle holds, biggest drawdown if it cracks. Deliberately sized below TSM because it is the most sentiment- and backlash-exposed name in the book.", sector: "Technology", name: "NVIDIA Corp" },
  V: { target: 12, role: "Off-factor diversifier", rationale: "Payments/stablecoin, own cycle; doesn't ride AI-capex sentiment.", thesis: "One of only two genuine decorrelators in the book. Payments run on their own cycle, so V cushions the ~60% of the portfolio fused to the AI-capex factor. Half of the V+CVX >= 20% off-factor floor.", sector: "Financials", name: "Visa Inc" },
  CVX: { target: 11, role: "Off-factor ballast", rationale: "Oil + natgas-to-AI-power leg (2.5 GW TX gas 2027); lowest beta to compute.", thesis: "The lowest-beta name to the compute trade, and the other half of the off-factor floor. Oil ballast plus a natgas-to-AI-power option (2.5 GW Texas gas, 2027) that quietly ties it back to the theme.", sector: "Energy", name: "Chevron Corp" },
  GEV: { target: 9, role: "Power high-beta call", rationale: "$150B backlog, inverse-sized for +235%/yr extension; conviction earns the thesis, not extra weight.", thesis: "The high-beta end of the power barbell: a $150B backlog and a huge extension runway. Inverse-sized on purpose, conviction is expressed by holding it at all, not by oversizing a name that can round-trip fast.", sector: "Industrials", name: "GE Vernova" },
  QCOM: { target: 6, role: "Cheap edge satellite", rationale: "~14x Snapdragon + AI200/AI250 inference; asymmetric, kept small.", thesis: "A cheap, asymmetric edge-inference call kept deliberately small. ~14x for Snapdragon plus the AI200/AI250 inference push: a satellite position, never a core one.", sector: "Technology", name: "Qualcomm Inc" },
  PLTR: { target: 2, role: "Dated-catalyst rental", rationale: "~115x prices perfection; rental only, flat between prints.", thesis: "A rental, not a holding. ~115x prices perfection, so the only edge is a dated catalyst: enter 3-5 days pre-print, exit on the print regardless. Hard-capped at 2% and flat between catalysts.", sector: "Technology", name: "Palantir Technologies" },
};

// Live weight (account basis) and unrealized P&L per held name, kept identical to the reconciliation
// fixture. Symbols absent here and absent from SLATE are treated as unknown (generic fallback).
interface LiveRow {
  weight: number;
  pl_pct: number;
  price: number;
  in_universe: boolean;
  in_slate: boolean;
}

const LIVE: Record<string, LiveRow> = {
  TSM: { weight: 15.9, pl_pct: 2.1, price: 210.4, in_universe: true, in_slate: true },
  VST: { weight: 9.5, pl_pct: -3.5, price: 145.8, in_universe: true, in_slate: true },
  NVDA: { weight: 11.0, pl_pct: 4.2, price: 178.6, in_universe: true, in_slate: true },
  V: { weight: 5.6, pl_pct: -1.8, price: 295.1, in_universe: true, in_slate: true },
  CVX: { weight: 4.8, pl_pct: 0.9, price: 152.3, in_universe: true, in_slate: true },
  QCOM: { weight: 5.2, pl_pct: -27.0, price: 168.9, in_universe: true, in_slate: true },
  MU: { weight: 6.9, pl_pct: -18.2, price: 108.4, in_universe: true, in_slate: false },
  SVRA: { weight: 2.5, pl_pct: -8.0, price: 6.42, in_universe: false, in_slate: false },
};

// Documented in the slate but not held (an exit or entry with no record).
const NOT_HELD = new Set(["GEV", "PLTR"]);

// A tiny deterministic PRNG (mulberry32) seeded from the symbol, so the mock price walk is stable
// across server and client renders. No Math.random(): that would mismatch on hydration.
function seedFrom(symbol: string): number {
  let h = 2166136261;
  for (const ch of symbol) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function isoMinusDays(endIso: string, days: number): string {
  const d = new Date(`${endIso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

// A ~90-business-day walk ENDING at `endPrice`, so the last point equals the live price. Built
// backward from the anchor with a gentle mean-reverting drift; weekends skipped.
function buildHistory(symbol: string, endPrice: number, days = 90): PricePoint[] {
  const rand = mulberry32(seedFrom(symbol));
  const points: PricePoint[] = [];
  let price = endPrice;
  let dayOffset = 0;
  for (let i = 0; i < days; i++) {
    // skip Sat/Sun so the axis reads like a real trading calendar
    let dateIso = isoMinusDays(HISTORY_END, dayOffset);
    let dow = new Date(`${dateIso}T00:00:00Z`).getUTCDay();
    while (dow === 0 || dow === 6) {
      dayOffset += 1;
      dateIso = isoMinusDays(HISTORY_END, dayOffset);
      dow = new Date(`${dateIso}T00:00:00Z`).getUTCDay();
    }
    points.push({ date: dateIso, close: Number(price.toFixed(2)) });
    // step backward: reverse a small daily return
    const shock = (rand() - 0.5) * 0.03; // +/- ~1.5% daily
    price = price / (1 + shock);
    dayOffset += 1;
  }
  return points.reverse();
}

function buildLive(row: LiveRow): PositionLive {
  const marketValue = Number(((row.weight / 100) * ACCOUNT_VALUE).toFixed(2));
  const quantity = Number((marketValue / row.price).toFixed(6));
  const costBasis = Number((marketValue / (1 + row.pl_pct / 100)).toFixed(2));
  const avg = Number((costBasis / quantity).toFixed(2));
  return {
    quantity,
    average_buy_price: avg,
    current_price: row.price,
    cost_basis: costBasis,
    market_value: marketValue,
    unrealized_pl: Number((marketValue - costBasis).toFixed(2)),
    unrealized_pl_pct: row.pl_pct,
    weight_account_pct: row.weight,
    weight_pct: null,
    priced: true,
  };
}

function buildStop(plPct: number, weight: number, target: number | null): StopState {
  const trimLine = target == null ? null : Number((target * 1.3).toFixed(1));
  return {
    hard_stop_pct: HARD_STOP_PCT,
    distance_to_stop_pct: Number((plPct - HARD_STOP_PCT).toFixed(1)),
    breached: plPct <= HARD_STOP_PCT,
    trim_line_weight_pct: trimLine,
    above_trim_line: trimLine != null && weight > trimLine,
  };
}

function thesisStatus(symbol: string, plPct: number | null, drift: number | null, inSlate: boolean): ThesisStatus {
  if (!inSlate) return "broken"; // held with no thesis on record
  if (plPct != null && plPct <= HARD_STOP_PCT) return "broken";
  if (plPct != null && plPct <= HARD_STOP_PCT + 5) return "watch";
  if (drift != null && Math.abs(drift) >= 5) return "watch";
  return "intact";
}

// A representative recent debate for a held slate name. Real records carry the full jury; the mock
// gives the verdict, both cases, and the tally so the card and the link read true.
function buildDebate(symbol: string): LinkedDebate | null {
  const slate = SLATE[symbol];
  if (!slate) return null;
  const breachedStop = (LIVE[symbol]?.pl_pct ?? 0) <= HARD_STOP_PCT;
  return {
    id: `dbt_${symbol.toLowerCase()}_2026-06-03`,
    created_at: "2026-06-03T18:00:00Z",
    question: `Does ${symbol} earn its ${slate.target}% slot in the barbell?`,
    decision: breachedStop ? "ESCALATED" : "BUY",
    escalated: breachedStop,
    bull_case: `${slate.thesis}`,
    bear_case: breachedStop
      ? `The name is ${LIVE[symbol]?.pl_pct}% underwater, past the -20% line. The catalyst has not landed and the thesis has to be re-underwritten, not averaged down.`
      : `Concentration risk: this leg shares the AI-capex factor with most of the book, so a capex air-pocket hits it and its neighbours together. The barbell hedges which end wins, not whether the cycle rolls over.`,
    jury_counts: breachedStop ? { BUY: 4, SELL: 1, HOLD: 5 } : { BUY: 7, SELL: 1, HOLD: 2 },
    jury_total: 10,
  };
}

export function buildMockPosition(symbolRaw: string): PositionDetailResponse {
  const symbol = symbolRaw.toUpperCase();
  const slate = SLATE[symbol];
  const live = LIVE[symbol];
  const held = !NOT_HELD.has(symbol) && !!live;
  const target = slate?.target ?? null;
  const weight = live?.weight ?? null;
  const drift = weight != null && target != null ? Number((weight - target).toFixed(1)) : null;
  const plPct = live?.pl_pct ?? null;
  const inSlate = slate != null || (live?.in_slate ?? false);
  const inUniverse = live?.in_universe ?? (slate != null);

  const referencePrice = live?.price ?? 100;

  return {
    meta: {
      symbol,
      name: slate?.name ?? (symbol === "MU" ? "Micron Technology" : symbol === "SVRA" ? "Savara Inc" : symbol),
      sector: slate?.sector ?? (symbol === "MU" ? "Technology" : null),
      snapshot_generated_at: SNAPSHOT_AT,
      snapshot_stale: true,
      price_source: "FMP",
      price_history_from: isoMinusDays(HISTORY_END, 126),
      held,
    },
    live: held ? buildLive(live) : null,
    slate: {
      in_slate: inSlate,
      in_universe: inUniverse,
      target_weight_pct: target,
      role: slate?.role ?? null,
      size_rationale: slate?.rationale ?? null,
      drift_pct: drift,
    },
    stop: held && plPct != null ? buildStop(plPct, weight ?? 0, target) : null,
    thesis: {
      status: thesisStatus(symbol, plPct, drift, inSlate),
      summary: slate?.thesis ?? (inSlate ? null : `${symbol} is held with no thesis on record. It is not in the documented slate; it needs a written case or an exit.`),
      updated_at: slate ? "2026-06-03T18:00:00Z" : null,
    },
    price_history: buildHistory(symbol, referencePrice),
    debate: buildDebate(symbol),
  };
}
