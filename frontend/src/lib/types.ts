// Mirrors the backend response shapes (backend/app/routers/*, app/debate/schemas.py).

export interface PositionView {
  symbol: string;
  quantity: number;
  average_buy_price: number;
  current_price: number | null;
  cost_basis: number;
  market_value: number | null;
  unrealized_pl: number | null;
  unrealized_pl_pct: number | null;
  /** Share of live equity value — priced positions only, EXCLUDES cash. Allocation mix. */
  weight_pct: number | null;
  /** Share of live account value (equity + cash). This is the basis the charter's ~25%-per-name
   *  cap is written against, so it is the one a cap breach may be judged on. Optional so an older
   *  backend that predates it degrades to "—" rather than rendering undefined. */
  weight_account_pct?: number | null;
  priced: boolean;
}

export interface AccountView {
  account_masked: string;
  nickname: string | null;
  generated_at: string;
  source: string;
  stale_prices: boolean;
  cash: number;
  buying_power: number;
  live_equity_value: number;
  live_total_value: number;
  total_cost_basis: number;
  total_unrealized_pl: number;
  total_unrealized_pl_pct: number | null;
  positions: PositionView[];
}

export interface RefreshStatus {
  pending: boolean;
  snapshot_generated_at: string | null;
  cooldown_remaining_s: number;
}

export type Vote = "BUY" | "SELL" | "HOLD";
export type Decision = "BUY" | "SELL" | "HOLD" | "ESCALATED";

export interface JurorVote {
  agent_id: number;
  focus_area: string;
  vote: Vote;
  confidence: number;
  reasoning: string;
  /**
   * Which model family judged this lens (from app/debate/schemas.py). Load-bearing on a paired
   * panel: the same lens is judged by both Claude and Gemini, so without it a disagreement is
   * unattributable. Optional here because records written before paired juries have no provider;
   * the backend defaults them to "anthropic", but a client reading an older cached payload may not
   * see the field at all.
   */
  provider?: string;
  model?: string;
}

/** Per-family vote counts and per-lens agreement on a paired panel, from
 *  app/debate/calibration.py::family_summary. Empty/absent for a single-family jury. A cross-family
 *  AGREEMENT is NOT extra confidence until there is a baseline (it may only mean the question was
 *  easy), so the UI reports it, it does not fold it into the verdict. */
export interface FamilySummary {
  /** provider -> { BUY, SELL, HOLD } counts. The sum per family is how many lenses it actually
   *  voted on, which is also how a family's abstentions show up (fewer than the paired count). */
  providers: Record<string, Partial<Record<Vote, number>>>;
  /** Lenses where BOTH families cast a vote (the ones a comparison is even possible on). */
  paired_lenses: number;
  lenses_agreed: number;
  /** lenses_agreed / paired_lenses, or null when no lens was paired. */
  agreement: number | null;
  /** Lens names where the families landed on different votes. */
  disagreed_on: string[];
}

/** Descriptive statistics for a panel's confidence, from app/debate/calibration.py. */
export interface ConfidenceSummary {
  n: number;
  mean: number | null;
  stdev: number | null;
  min: number | null;
  max: number | null;
  /**
   * False when the numbers are present but carry no information — the panel returned effectively
   * one value. Measured before this existed: 0.72 on 59% of every vote ever cast, and 8 of 10
   * jurors in a single debate. A confidence bar drawn from a constant asserts a measurement that
   * nothing made, so the page renders the raw number and says why instead.
   */
  usable: boolean;
}

export interface JuryResult {
  votes: JurorVote[];
  counts: Record<string, number>;
  decision: Decision;
  escalated_to_human: boolean;
  reason: string;
  /**
   * Ways this panel's output should be read with suspicion — confidence constant across ten
   * lenses, a value repeated verbatim by most jurors, uniform certainty. Empty is healthy. These
   * ANNOTATE: `decision` above is a vote count and is unchanged by them, which is what keeps
   * verdicts comparable once Claude and Gemini jurors sit on the same panel.
   */
  calibration_signals?: string[];
  confidence?: ConfidenceSummary;
  /** Present when more than one model family sat on the panel; absent/empty otherwise. */
  families?: FamilySummary;
}

export interface DebateSummary {
  id: string;
  ticker: string | null;
  created_at: string;
  question: string;
  decision: Decision | null;
  escalated: boolean;
  source: string;
}

export interface BullBear {
  bull_case: string;
  bear_case: string;
}

/** One thing one side said, in order.
 *
 *  The exchange, not just the conclusions. `bull_bear` above holds the round-1 OPENINGS only —
 *  which was the whole record while the two researchers wrote concurrently and never saw each
 *  other. With rebuttal rounds the order carries the meaning: a concession in round 2 is only
 *  legible next to the claim in round 1 that forced it. */
export interface DebateTurn {
  round_no: number;
  side: "bull" | "bear";
  kind: "opening" | "rebuttal" | "closing";
  content: string;
}

/** The fundamentals shape produced by src/data.py::fundamentals_from_info. Every field optional —
 *  yfinance misses fields routinely and older records may predate some of them. */
export interface FundamentalsData {
  market_cap?: number | null;
  peg?: number | null;
  fcf_yield?: number | null;
  free_cash_flow?: number | null;
  net_income?: number | null;
  operating_cash_flow?: number | null;
  gross_margin?: number | null;
  revenue_growth?: number | null;
  name?: string | null;
  sector?: string | null;
  industry?: string | null;
  trailing_pe?: number | null;
  forward_pe?: number | null;
  price?: number | null;
  ticker?: string;
}

/** Full record from GET /api/debate/{id}. Engine debates carry the structured fields; archived
 *  hand-written debates carry only `markdown` (plus id/source), so everything else is optional. */
export interface DebateDetail {
  id: string;
  source: string;
  ticker?: string | null;
  created_at?: string;
  question?: string;
  price?: number | null;
  fundamentals?: FundamentalsData | null;
  bull_bear?: BullBear | null;
  /** Empty on records written before rebuttals existed, and on hand-written archive entries — so
   *  the page falls back to bull_bear rather than assuming this is populated. */
  turns?: DebateTurn[];
  rounds?: number;
  jury?: JuryResult | null;
  final_decision?: Decision | null;
  position_size_note?: string | null;
  models?: Record<string, string>;
  /** Raw markdown body — archive records only. */
  markdown?: string;
}

/** One row from GET /api/pipeline/history (issue #28): a persisted pipeline run with the
 *  entry-vs-current price comparison computed server-side. Backed by the interim JSONL store
 *  (see backend/app/debate/records.py) until the DB evaluation tables are wired. */
export interface PipelineRunView {
  id: string;
  ticker: string;
  created_at: string;
  /** Links to /debate/[id] for the full stage-by-stage record of the wrapped debate. */
  debate_id: string | null;
  price_at_run: number | null;
  screen_passed: boolean | null;
  screen_composite: number | null;
  screen_reason: string | null;
  decision: Decision | null;
  escalated: boolean;
  /** Live mark from the shared yfinance layer; null when the symbol couldn't be priced. */
  current_price: number | null;
  /** current_price - price_at_run, dollars. Null when either side is missing. */
  delta: number | null;
  /** Same move in percent of the entry price. */
  delta_pct: number | null;
  priced: boolean;
}

// ---------------------------------------------------------------------------
// Auth wire shapes — docs/AUTH_THREAT_MODEL.md §4. These mirror the specified
// contract, not a running backend (none exists yet); any drift from §4 is a bug
// here, not there. Flow-level result unions live in lib/auth.ts.
// ---------------------------------------------------------------------------

/** POST /api/auth/login response body. Success is `status: "mfa_required"` WITH
 *  a challenge_token; every password-step failure arrives in the IDENTICAL shape
 *  (§5.2 — unknown address and wrong password must be indistinguishable), so the
 *  token fields are optional and the client must not branch on anything finer
 *  than "mfa_required with a token" vs "not". */
export interface LoginStepResponse {
  status: string;
  challenge_token?: string;
  /** Challenge TTL in seconds. The challenge only confers the right to attempt
   *  the TOTP step — it is never a session (§4). */
  expires_in?: number;
}

/** GET /api/auth/me — the only source of client-side auth state. The session
 *  cookie (__Host-rh_sid) is HttpOnly and deliberately unreadable from JS. */
export interface MeResponse {
  email: string;
  /** §5.11: an unverified address is flagged here so the UI can surface it.
   *  It is NOT a login gate — fail toward the operator retaining access. */
  email_verified: boolean;
}

/** POST /api/auth/verify response. A racing double-consume loses gracefully and
 *  resolves to a friendly "already_verified" (§5.6). */
export interface VerifyResponse {
  status: "verified" | "already_verified" | string;
}

export interface ScanResult {
  ticker: string;
  ok: boolean;
  passed: boolean;
  failed_tier?: string | null;
  composite?: number | null;
  reason?: string | null;
  peg?: number | null;
  fcf_yield?: number | null;
  name?: string | null;
  sector?: string | null;
  industry?: string | null;
  market_cap?: number | null;
  price?: number | null;
  trailing_pe?: number | null;
  forward_pe?: number | null;
  gross_margin?: number | null;
  revenue_growth?: number | null;
}
