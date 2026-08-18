// Types for `GET /api/fundamentals` — the full fundamental set with history.
//
// UNITS ARE NOT UNIFORM, AND THAT IS THE MAIN HAZARD HERE
//   Margins, growth rates, ROE/ROC, R&D intensity and equity/assets arrive as FRACTIONS (0.469 =
//   46.9%). Current ratio, quick ratio, debt/equity, interest coverage and per-share figures are
//   plain numbers. Cash conversion cycle is DAYS. Rendering a fraction with the percent formatter
//   for one field and not another is how a page ends up claiming 0.47% gross margin for Apple, so
//   every field below is routed through an explicit format in FIELD_GROUPS rather than guessed
//   from its value.
//
// TWO ROW KINDS
//   `market` is as of a fetch; `latest_annual` and `history` are as of a filing. The endpoint keeps
//   them apart and so does the page — each block states when it was true.

export interface PiotroskiSignals {
  /** Signals that came out true. Out of `evaluated`, NOT out of `of`. */
  score: number;
  /** The denominator the variant defines — 9 for Cary's. */
  of: number;
  /** How many of the nine could be computed at all. Below `of` means inputs were missing. */
  evaluated: number;
  /** True only when every signal was computable. A partial score is not an F-score. */
  complete: boolean;
  variant: string;
  periods: { prior: string | null; current: string | null };
  /** null = the inputs for this signal were missing, which is NOT the same as a failed signal. */
  signals: Record<string, boolean | null>;
}

export interface AnnualPeriod {
  period_end: string;
  /** The filing's acceptance date — what makes this row point-in-time safe. */
  known_at: string;
  revenue_ttm: number | null;
  ebitda_ttm: number | null;
  eps_current: number | null;
  eps_growth_yoy: number | null;
  free_cash_flow: number | null;
  fcf_yield: number | null;
  capital_expenditure: number | null;
  net_debt: number | null;
  shares_outstanding: number | null;
  gross_margin: number | null;
  operating_margin: number | null;
  net_margin: number | null;
  ebitda_margin: number | null;
  roe: number | null;
  roc: number | null;
  current_ratio: number | null;
  quick_ratio: number | null;
  debt_to_equity: number | null;
  equity_to_assets: number | null;
  ebitda_interest: number | null;
  cash_conversion_cycle: number | null;
  revenue_growth_yoy: number | null;
  rd_to_revenue: number | null;
  tangible_book_value_per_share: number | null;
  /** null when the nine signals could not all be computed — see PiotroskiSignals.complete. */
  piotroski_f_score: number | null;
  piotroski_variant: string | null;
  piotroski_signals: PiotroskiSignals | null;
  /** field -> the formula used, for anything we calculated rather than received. */
  derived_fields: Record<string, string> | null;
}

export interface MarketBlock {
  period_end: string;
  known_at: string;
  price: number | null;
  market_cap: number | null;
  pe_trailing: number | null;
  pe_forward: number | null;
  peg_ratio: number | null;
  price_to_book: number | null;
  price_to_sales: number | null;
  price_to_tangible_book: number | null;
  ev_to_ebitda: number | null;
  dividend_yield: number | null;
  beta: number | null;
  week_52_high: number | null;
  week_52_low: number | null;
  avg_volume_30d: number | null;
  analyst_target_price: number | null;
  analyst_recommendation: string | null;
}

export interface FundamentalsRow {
  symbol: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  market: MarketBlock | null;
  latest_annual: AnnualPeriod | null;
  /** Newest first. */
  history: AnnualPeriod[];
  periods_on_record: number;
  /** Present when the security is known but nothing has been ingested for it. */
  meta_note?: string;
}

export interface FundamentalsResponse {
  meta: {
    requested: string[];
    /** Requested symbols absent from `securities` — named rather than silently dropped. */
    unknown_symbols: string[];
    count: number;
    piotroski_variant: string;
  };
  rows: FundamentalsRow[];
}

export type Unit = "usd" | "usdCompact" | "fraction" | "ratio" | "days" | "shares" | "int";

export interface FieldSpec {
  key: keyof AnnualPeriod | keyof MarketBlock;
  label: string;
  unit: Unit;
  /** Higher is better (true), lower is better (false), or neither (undefined). Drives the trend
   *  arrow colour in the history table — and is left undefined wherever "better" is genuinely
   *  contestable (net debt in a rate-cutting cycle, capex for a company in a build-out). */
  higherIsBetter?: boolean;
}

/** The owner's sheet, grouped. Order within a group is the reading order, not the column order. */
export const ANNUAL_GROUPS: { title: string; fields: FieldSpec[] }[] = [
  {
    title: "Scale & growth",
    fields: [
      { key: "revenue_ttm", label: "Revenue", unit: "usdCompact", higherIsBetter: true },
      { key: "revenue_growth_yoy", label: "Revenue growth YoY", unit: "fraction", higherIsBetter: true },
      { key: "ebitda_ttm", label: "EBITDA", unit: "usdCompact", higherIsBetter: true },
      { key: "eps_current", label: "EPS", unit: "usd", higherIsBetter: true },
      { key: "eps_growth_yoy", label: "EPS growth YoY", unit: "fraction", higherIsBetter: true },
      { key: "shares_outstanding", label: "Shares out", unit: "shares", higherIsBetter: false },
    ],
  },
  {
    title: "Margins & returns",
    fields: [
      { key: "gross_margin", label: "Gross margin", unit: "fraction", higherIsBetter: true },
      { key: "operating_margin", label: "Operating margin", unit: "fraction", higherIsBetter: true },
      { key: "ebitda_margin", label: "EBITDA margin", unit: "fraction", higherIsBetter: true },
      { key: "net_margin", label: "Net margin", unit: "fraction", higherIsBetter: true },
      { key: "roe", label: "Return on equity", unit: "fraction", higherIsBetter: true },
      { key: "roc", label: "Return on capital", unit: "fraction", higherIsBetter: true },
    ],
  },
  {
    title: "Cash flow",
    fields: [
      { key: "free_cash_flow", label: "Free cash flow", unit: "usdCompact", higherIsBetter: true },
      { key: "capital_expenditure", label: "Capex", unit: "usdCompact" },
      { key: "fcf_yield", label: "FCF yield", unit: "fraction", higherIsBetter: true },
      { key: "cash_conversion_cycle", label: "Cash conversion cycle", unit: "days", higherIsBetter: false },
      { key: "rd_to_revenue", label: "R&D / revenue", unit: "fraction" },
    ],
  },
  {
    title: "Balance sheet",
    fields: [
      { key: "net_debt", label: "Net debt", unit: "usdCompact" },
      { key: "debt_to_equity", label: "Debt / equity", unit: "ratio", higherIsBetter: false },
      { key: "equity_to_assets", label: "Equity / assets", unit: "fraction", higherIsBetter: true },
      { key: "current_ratio", label: "Current ratio", unit: "ratio", higherIsBetter: true },
      { key: "quick_ratio", label: "Quick ratio", unit: "ratio", higherIsBetter: true },
      { key: "ebitda_interest", label: "Interest coverage", unit: "ratio", higherIsBetter: true },
      { key: "tangible_book_value_per_share", label: "Tangible book / share", unit: "usd", higherIsBetter: true },
    ],
  },
];

export const MARKET_FIELDS: FieldSpec[] = [
  { key: "price", label: "Price", unit: "usd" },
  { key: "market_cap", label: "Market cap", unit: "usdCompact" },
  { key: "pe_trailing", label: "P/E trailing", unit: "ratio" },
  { key: "pe_forward", label: "P/E forward", unit: "ratio" },
  { key: "peg_ratio", label: "PEG", unit: "ratio" },
  { key: "price_to_book", label: "P/B", unit: "ratio" },
  { key: "price_to_sales", label: "P/S", unit: "ratio" },
  { key: "price_to_tangible_book", label: "P/TBV", unit: "ratio" },
  { key: "ev_to_ebitda", label: "EV/EBITDA", unit: "ratio" },
  { key: "dividend_yield", label: "Dividend yield", unit: "fraction" },
  { key: "beta", label: "Beta", unit: "ratio" },
  { key: "week_52_high", label: "52w high", unit: "usd" },
  { key: "week_52_low", label: "52w low", unit: "usd" },
  { key: "avg_volume_30d", label: "Avg volume 30d", unit: "int" },
  { key: "analyst_target_price", label: "Analyst target", unit: "usd" },
];

/** Cary's nine, in the order the professor's Bloomberg code evaluates them. */
export const PIOTROSKI_LABELS: Record<string, string> = {
  roa_improved: "ROA improved",
  cfo_improved: "Operating cash flow improved",
  net_income_improved: "Net income improved",
  cfo_exceeds_net_income: "Cash flow exceeds net income",
  leverage_fell: "Leverage fell",
  current_ratio_improved: "Current ratio improved",
  shares_not_diluted: "No share dilution",
  gross_margin_improved: "Gross margin improved",
  asset_turnover_improved: "Asset turnover improved",
};

/** Fields we know this data plan cannot supply, so the page states it instead of showing a gap. */
export const KNOWN_ABSENT = [
  "Short interest",
  "Next-year EPS estimate",
  "Insider ownership",
  "Institutional ownership",
];
