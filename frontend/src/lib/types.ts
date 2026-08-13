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
  snapshot_total_value: number;
  snapshot_equity_value: number;
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
}

export interface JuryResult {
  votes: JurorVote[];
  counts: Record<string, number>;
  decision: Decision;
  escalated_to_human: boolean;
  reason: string;
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
