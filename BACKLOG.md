# Feature Backlog — Agentic Dashboard

Requested features, captured for later. Not yet built. Ordered roughly by independence (low → high
risk). Each item notes where it touches and what it needs.

---

## 1. Debate detail page (click a debate → full stage-by-stage view)

**What:** On the Debate page, clicking a row in the history (or the just-run result) opens a dedicated
page for that debate (`/debate/[id]`) showing **every part of every stage**: the bull case, the bear
case, all 10 juror votes (full reasoning + confidence, not truncated), the aggregate counts, the final
decision, and the position-size note. For archived (hand-written) debates, render their markdown.

**Plus a summary table** on that page showing what's **BUY vs SELL (vs HOLD)** — the per-juror
vote breakdown in a table, and (where the debate covers a slate) which tickers are buy vs sell.

**Where / notes:**
- Backend already serves the full record: `GET /api/debate/{id}` (engine debates return structured
  JSON; archives return raw markdown). Mostly a **frontend** task: a new `frontend/src/app/debate/[id]/page.tsx`,
  link each history row to it.
- Reuse the juror-grid + vote-tally components from the current debate page.

**Effort:** Small–medium. Frontend-only.

---

## 2. "Commit" button — actually place the trades from a debate  ⚠ BIGGEST ITEM

**What:** A green **Commit** button in each debate-history row, positioned between the **source**
(engine/archive) badge and the **"N days ago"** timestamp. Clicking it executes the trade(s) implied
by that debate's decision and shows a **summary of the trade**.

**Why this is the heavy one (flagging so we build it right):**
- It crosses the app's deliberate **read-only** boundary — today there is *no* order-placement path
  anywhere, by design. This adds one.
- The container **cannot reach the Robinhood MCP** (same constraint as the refresh bridge). Placing an
  order needs `place_equity_order`, which only exists in a host-side Claude+MCP session. So Commit would
  reuse the **host bridge pattern**: button → backend writes a `trade.request` → host daemon →
  `claude` runs `review_equity_order` → **human confirm** → `place_equity_order` → writes a fill
  record back → the button shows the summary.
- Must honor the charter's risk rules (≤25%/name, 10–20% cash floor, exit-before-entry, no averaging
  down) and be **idempotent** (no double-fills on a double-click). Confirmation is mandatory — this
  spends real money on the live account.

**Where / notes:** new backend `trade` router + a `bin/trade_daemon.sh` (or extend the refresh daemon),
the charter guardrails as pre-trade checks, a fills log, and the frontend button + summary modal.

**Effort:** Large. Security- + money-sensitive — should go through its own `/fixpass`.

---

## 3. Pipeline history with price comparison

**What:** On the Pipeline page, a **history of tickers** that have been (and are) run through the
pipeline. For each, show the **current-day price** (yfinance) and a **comparison to the price when it
was debated / entered the pipeline** (entry price → current price, $ and % change).

**Where / notes:**
- Pipeline runs aren't persisted as their own record yet (the pipeline reuses the debate engine, which
  persists a `DebateRecord` — the screen node already captures the price-at-run). Add a small
  **pipeline-run store** (ticker, timestamp, price-at-run, decision) — or derive it from the debate
  records the pipeline already writes.
- Add a "current price" lookup (reuse `services/marks.py`) and a history endpoint; frontend renders the
  table with the delta.

**Effort:** Medium. Backend (persist + endpoint) + frontend table.

---

## 4. Scan → full fundamentals view

**What:** Make the Scan page show **all the fundamentals** per company — ratios, prices, etc.
(market cap, PEG, trailing & forward P/E, FCF yield, gross margin, revenue growth, sector/industry,
current price …), not just the pass/fail + PEG/FCF it shows today.

**Where / notes:**
- `src/data.py::fundamentals_from_info` already fetches most of these fields — they're just not all
  surfaced in the scan result payload. Widen `scan.py`'s `_screen_one` output + the frontend table
  (consider expandable rows or a detail drawer per ticker).

**Effort:** Small–medium. Mostly plumbing existing data through.

---

## 5. Portfolio interactive value chart

**What:** On the Portfolio page, a **bar chart of portfolio value over time** that's interactive:
preset ranges (1D, 1W, 1M, 3M, 1Y, All) **and** a movable/custom span (e.g. "2 weeks", or a specific
date range).

**Plus benchmark overlays:** toggle **S&P 500, QQQ (Nasdaq-100), and Dow Jones** on/off to compare
the portfolio against the market over the same range.

**Where / notes:**
- Needs a **time-series of portfolio value**, which we don't store yet (only point-in-time snapshots).
  Options to source it:
  1. Have the twice-daily **cycle job append a value point** to a `logs/portfolio_value.jsonl` each
     open/close (simple, forward-only — history builds over time).
  2. Use **Robinhood `get_portfolio_historicals`** via the MCP bridge (real historical account value).
  3. **Synthesize** a curve from yfinance historical prices × current holdings (approximate, but
     instant history).
  A pragmatic combo: synthesize past history (option 3) now, and accumulate real points (option 1) going
  forward.
- **Benchmarks** come from yfinance historicals: S&P 500 (`^GSPC` or `SPY`), Nasdaq-100 (`QQQ`),
  Dow Jones (`^DJI` or `DIA`). Backend endpoint to fetch index series for the selected range; cache them.
- **Normalize for comparison:** a $198 portfolio can't be plotted against index point levels directly —
  rebase both the portfolio and each benchmark to **% return from the start of the selected range**
  (start = 0%) so the lines are comparable. (This likely means the comparison view is a normalized
  %-return line chart even if the standalone value view is bars.)
- Frontend: Recharts `Brush` gives the movable span; preset range buttons filter the series; benchmark
  toggles add/remove overlay series.

**Effort:** Medium–large (data-source decision + history backfill + benchmark fetch/normalize).

---

### Cross-cutting note
Items 2 and 5 both want the **host-side MCP bridge** (place orders / pull RH historicals) — the same
pattern as the existing refresh bridge. Worth building that bridge as one reusable "host MCP action"
mechanism when we tackle either, rather than twice.
