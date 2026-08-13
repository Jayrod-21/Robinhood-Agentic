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
}
