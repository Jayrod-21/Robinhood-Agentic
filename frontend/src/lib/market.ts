// Types plus a dev-only fixture for the Market page: the read-only market-context feed sourced from
// the Market Mover daily brief. Two things the slate actually acts on: a catalyst calendar flagged
// against held/slate names (the slate runs PLTR as a rental, "enter 3-5 days pre-print, exit on the
// print", and cuts high-beta legs on a capex guide-down), and a market headline feed for the names
// in the book. A one-line macro read sits on top.
//
// Mirrors the PROPOSED `GET /api/market-context` contract in docs/contracts/market-context-endpoint.md.
// Market Mover ([[project_market_mover]]) is a separate project; per ADR-001 the trading backend's
// Postgres has NO network port, so the brief reaches it via the app/brief bridge (ingested brief
// output), never a live cross-DB link. The frontend does not care how; it reads one bundled route.
//
// Not in lib/types.ts on purpose (same reason as the other mock-gated pages): that file mirrors
// shapes a real backend returns today, and this endpoint does not exist yet.

export type Sentiment = "positive" | "negative" | "neutral";

export interface Headline {
  id: string;
  title: string;
  /** Upstream source name as the brief recorded it (e.g. "Reuters"), surfaced so it is auditable. */
  source: string;
  url: string | null;
  published_at: string;
  summary: string | null;
  /** Held/slate names this headline mentions; drives the relevance chips. */
  tickers: string[];
  sentiment: Sentiment | null;
}

// earnings/print : a company report (the rental/print discipline keys on these)
// econ           : a macro print (CPI, FOMC) with no single ticker
// product/other  : a dated product or event catalyst
export type CatalystType = "earnings" | "print" | "econ" | "product" | "other";

export interface Catalyst {
  /** Null for a macro/econ catalyst (CPI, FOMC) that isn't tied to one name. */
  symbol: string | null;
  label: string;
  type: CatalystType;
  date: string;
  /** Backend-computed trading days until the catalyst, so the client never runs Date.now(). */
  days_until: number | null;
  in_slate: boolean;
  held: boolean;
  /** The slate enters rental names (PLTR) 3-5 days pre-catalyst; true when we're in that window. */
  rental_window: boolean;
  note: string | null;
}

// A ranked market-mover pick from the brief: the daily "what moved and why", in the brief's own
// order. Sourced from Market Mover's latest.json briefing records; text fields are third-party and
// rendered as data (never trusted), same rule as the headlines.
export interface TopMover {
  /** 1-based rank within the day's brief. */
  rank: number;
  /** The name the pick is about; null for a macro/thematic mover with no single ticker. */
  ticker: string | null;
  /** The brief's category tag for the pick (e.g. "AI hardware", "Macro"); null when none. */
  category: string | null;
  title: string;
  /** The brief's one-line reason for the pick; null when none. */
  justification: string | null;
  /** Market Mover's own verdict label, passed through verbatim as data; null when none. */
  verdict: string | null;
}

export interface MarketMeta {
  brief_generated_at: string | null;
  /** The brief is older than a trading day; act on it with that caveat. */
  brief_stale: boolean;
  source: string;
  /** One or two sentences of market read from the brief; null when the brief carries none. */
  macro_read: string | null;
}

export interface MarketContextResponse {
  meta: MarketMeta;
  /** The brief's ranked movers. Optional so a backend that has not added the field yet degrades to
   *  an empty section rather than throwing. */
  top_movers?: TopMover[];
  catalysts: Catalyst[];
  headlines: Headline[];
}

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_MARKET_MOCK=1); the default hits the real endpoint. Anchored to a "today"
// of 2026-08-16 so days_until reads consistently; names/stances match the slate and reconciliation
// fixtures (PLTR is a not-held rental with a catalyst 4 days out = a live signal; QCOM is the
// stop-breached name in the headlines).
export const MARKET_MOCK = process.env.NEXT_PUBLIC_MARKET_MOCK === "1";

const CATALYSTS: Catalyst[] = [
  {
    symbol: "PLTR",
    label: "Q3 earnings",
    type: "earnings",
    date: "2026-08-20",
    days_until: 4,
    in_slate: true,
    held: false,
    rental_window: true,
    note: "Rental window open: the slate enters 3-5 days pre-print and exits on the print. Not currently held.",
  },
  {
    symbol: null,
    label: "CPI print",
    type: "econ",
    date: "2026-08-18",
    days_until: 2,
    in_slate: false,
    held: false,
    rental_window: false,
    note: "A hot print pressures the AI-capex factor the book is ~60% fused to. V/CVX/cash are the cushion.",
  },
  {
    symbol: null,
    label: "FOMC minutes",
    type: "econ",
    date: "2026-08-20",
    days_until: 4,
    in_slate: false,
    held: false,
    rental_window: false,
    note: null,
  },
  {
    symbol: "NVDA",
    label: "Q2 earnings",
    type: "earnings",
    date: "2026-08-27",
    days_until: 11,
    in_slate: true,
    held: true,
    rental_window: false,
    note: "The book's convexity leg reports; highest single-name event risk in the slate.",
  },
  {
    symbol: "VST",
    label: "Investor day",
    type: "product",
    date: "2026-09-04",
    days_until: 19,
    in_slate: true,
    held: true,
    rental_window: false,
    note: null,
  },
];

const HEADLINES: Headline[] = [
  {
    id: "mm-2026-08-16-1",
    title: "TSMC lifts full-year outlook on sustained AI chip demand",
    source: "Reuters",
    url: null,
    published_at: "2026-08-16T11:20:00Z",
    summary: "The foundry raised guidance, citing advanced-node orders; read-through to the compute anchor and its customers.",
    tickers: ["TSM", "NVDA"],
    sentiment: "positive",
  },
  {
    id: "mm-2026-08-16-2",
    title: "Chip stocks slip as capex-cycle jitters resurface ahead of CPI",
    source: "Bloomberg",
    url: null,
    published_at: "2026-08-16T09:05:00Z",
    summary: "Semis and AI-power names sold off together, the exact correlation the barbell is built to survive.",
    tickers: ["NVDA", "TSM", "VST"],
    sentiment: "negative",
  },
  {
    id: "mm-2026-08-15-1",
    title: "Vistra signs another data-center power agreement",
    source: "Market Mover",
    url: null,
    published_at: "2026-08-15T21:40:00Z",
    summary: "Adds to the unguided PPA option the thesis sized VST above GEV for.",
    tickers: ["VST"],
    sentiment: "positive",
  },
  {
    id: "mm-2026-08-15-2",
    title: "Qualcomm shares extend slump after soft guidance",
    source: "CNBC",
    url: null,
    published_at: "2026-08-15T18:12:00Z",
    summary: "The cheap edge satellite is the name past its -20% stop; the tape keeps confirming the break, not the thesis.",
    tickers: ["QCOM"],
    sentiment: "negative",
  },
  {
    id: "mm-2026-08-15-3",
    title: "Palantir climbs into earnings on new government award",
    source: "Reuters",
    url: null,
    published_at: "2026-08-15T14:30:00Z",
    summary: "Relevant only as a rental: the slate plays the print, not the story.",
    tickers: ["PLTR"],
    sentiment: "positive",
  },
  {
    id: "mm-2026-08-14-1",
    title: "Fed officials signal caution as inflation data looms",
    source: "WSJ",
    url: null,
    published_at: "2026-08-14T22:00:00Z",
    summary: "Sets up the CPI print two days out as the near-term macro pivot for the whole book.",
    tickers: [],
    sentiment: "neutral",
  },
];

const TOP_MOVERS: TopMover[] = [
  {
    rank: 1,
    ticker: "TSM",
    category: "AI hardware",
    title: "TSMC lifts full-year outlook on sustained AI chip demand",
    justification: "Advanced-node orders read straight through to the compute anchor; the strongest confirming signal in the book today.",
    verdict: "bullish",
  },
  {
    rank: 2,
    ticker: "VST",
    category: "AI power",
    title: "Vistra signs another data-center power agreement",
    justification: "Adds to the unguided PPA option the thesis sized VST above GEV for.",
    verdict: "bullish",
  },
  {
    rank: 3,
    ticker: "QCOM",
    category: "Semis",
    title: "Qualcomm slump extends after soft guidance",
    justification: "The name is past its -20% stop; the tape keeps confirming the break, not the thesis.",
    verdict: "bearish",
  },
  {
    rank: 4,
    ticker: null,
    category: "Macro",
    title: "Fed officials signal caution as CPI looms",
    justification: "Frames the CPI print two days out as the near-term pivot for the whole book.",
    verdict: "neutral",
  },
];

export const MOCK_MARKET_CONTEXT: MarketContextResponse = {
  meta: {
    brief_generated_at: "2026-08-16T12:30:00Z",
    brief_stale: false,
    source: "Market Mover",
    macro_read:
      "AI-capex sentiment is wobbling into a CPI print two days out; the power leg (VST/CVX) is holding better than the compute leg. Two held names report within two weeks, and PLTR's rental window is open.",
  },
  top_movers: TOP_MOVERS,
  catalysts: CATALYSTS,
  headlines: HEADLINES,
};
