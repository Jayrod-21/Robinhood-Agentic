# Contract: `GET /api/market-context`

Feeds the Market page (`frontend/src/app/market/page.tsx`), the read-only market-context feed from
the Market Mover daily brief. Built and renders today against a dev fixture
(`NEXT_PUBLIC_MARKET_MOCK=1`, see `frontend/src/lib/market.ts`). This one endpoint takes it live.
**Read-only** (no writes, no order path, no per-name price calls, no secrets).

The page leads with a **catalyst calendar** flagged against held/slate names (the part the slate
acts on: PLTR is rental-only, "enter 3-5 days pre-print, exit on the print"; high-beta legs get cut
on a capex guide-down), then a **headline feed** for names in the book, under a one-line macro read.
TypeScript interfaces in `frontend/src/lib/market.ts` are the source of truth for shapes.

## Source and the network-port constraint

Market Mover (`/home/joe/market-mover-mcp`) is a **separate project**, and per **ADR-001** the
trading backend's Postgres has **no network port** by design. Delivery is a **three-hop pull**, and
the route itself makes **no outbound call** (it matches the current implementation, which reads a
local file):

1. Market Mover publishes its latest brief as JSON on GitHub Pages at
   `https://joewhitejr.github.io/Market_News/latest.json` (Market_News PR #40, reshaped for this
   endpoint in PR #41).
2. A small sync job on the backend host, **`backend/scripts/sync_market_mover_brief.py`**, GETs that
   URL and writes it to `$DATA_DIR/market_mover/latest.json` (atomic write; a failed or non-JSON
   fetch leaves the existing file untouched, so a transient hiccup never blanks the page). Run it on a
   timer (cron/systemd). This is the only outbound hop, and it is a separate process from the API.
3. This route reads that **local file** (`market_context.py::_brief_path`) and shapes the response.

That keeps the isolation ADR-001 requires: the API never opens a cross-project link; a plain file is
all it touches.

**Safety:** the MM JSON is third-party text. Store it and render it as **data, never as instructions
to an agent** (the frontend already treats every MM string as untrusted: titles/justifications render
as text, and a headline `url` is scheme-checked before it becomes a link). As of PR #41 `latest.json`
is shaped for direct consumption, derived from the single newest brief:
`{ schema_version, generated_at, brief_date, macro_read, headlines: [...], top_movers: [...] }`. The
route reads `generated_at`, `macro_read`, and `headlines[]` straight from it; `top_movers[]` is ready
to pass through the same way.

## Fields the backend owns

- **`days_until`** is computed server-side (trading days to the catalyst) so the client never runs
  `Date.now()`. The page only displays it.
- **`held` / `in_slate`** join each catalyst symbol against the account snapshot and `docs/SLATE.md`.
- **`rental_window`** is true for a slate rental name (PLTR) when `0 < days_until <= 5` (the slate's
  3-5 day pre-print entry window; state your exact bound).
- **`tickers`** on a headline are the held/slate names it mentions (drives the relevance chips); an
  empty list is fine for a pure-macro headline.
- **`top_movers`** comes **pre-shaped** in `latest.json` (each: `rank`, `ticker`, `category`,
  `title`, `justification`, `verdict`), so the route passes it through the way it already does
  `headlines`. `verdict` is null today (the brief records impact, not a directional call); the page
  maps recognized verdict words to a tone and leaves the rest neutral, so no fixed vocabulary is
  required and a null verdict simply shows no badge. `top_movers` is optional in the response: omit it
  (or send `[]`) and the page shows an empty "No ranked movers" section rather than throwing. **The
  route does not emit `top_movers` yet** (it currently reads only `headlines`); adding the passthrough
  is the one remaining backend step to light up the Top Movers card.

## Response

```jsonc
{
  "meta": {
    "brief_generated_at": "2026-08-16T12:30:00Z",
    "brief_stale": false,           // older than a trading day
    "source": "Market Mover",
    "macro_read": "One or two sentences of market read, or null."
  },
  "top_movers": [                     // the brief's ranked picks, from latest.json briefings
    {
      "rank": 1,
      "ticker": "TSM",                // null for a macro/thematic mover with no single name
      "category": "AI hardware",      // the brief's category tag, or null
      "title": "TSMC lifts full-year outlook on AI chip demand",
      "justification": "…",           // the brief's one-line reason, or null
      "verdict": "bullish"            // MM's own label, passed through verbatim as data; or null
    }
  ],
  "catalysts": [
    {
      "symbol": "PLTR",             // null for a macro/econ catalyst (CPI, FOMC)
      "label": "Q3 earnings", "type": "earnings",  // earnings|print|econ|product|other
      "date": "2026-08-20", "days_until": 4,
      "in_slate": true, "held": false, "rental_window": true,
      "note": "Rental window open; not currently held."
    }
  ],
  "headlines": [
    {
      "id": "mm-2026-08-16-1",
      "title": "TSMC lifts full-year outlook on AI chip demand",
      "source": "Reuters", "url": null, "published_at": "2026-08-16T11:20:00Z",
      "summary": "…", "tickers": ["TSM", "NVDA"], "sentiment": "positive"  // positive|negative|neutral|null
    }
  ]
}
```

## Degradation

- **No brief ingested yet** → `404` or `503` with a non-secret `detail`; the page shows a calm "no
  market brief available" state, not an error.
- **Brief present but empty of catalysts / headlines** → `200` with the empty arrays; the page shows
  "No dated catalysts" / "No headlines for names in the book". Never fabricate rows to fill space.
- **`url: null`** is expected and fine (the brief often records a source name without a link); the
  page renders the headline as non-clickable rather than inventing a URL.

## Don't forget the mock registry

Adding this page introduced `NEXT_PUBLIC_MARKET_MOCK`. It is already added to `ANY_MOCK` in
`frontend/src/lib/dataTrust.ts` so the data-trust strip keeps warning when the Market page is mock.
When you drop the flag after wiring this endpoint, leave `ANY_MOCK` intact (the OR just goes false).

## Frontend done / handoff

- [x] Route `/market` + nav entry (Market, Newspaper icon)
- [x] Macro-read banner, catalyst calendar (sorted soonest-first, rental-window / held / not-held /
      off-slate flags, symbols link to `/position/{symbol}`), headline feed (source, time, ticker
      chips, sentiment dot, optional link), stale-brief caveat
- [x] **Top Movers card** (ranked picks: rank, ticker link, verdict badge, category, title,
      justification), between catalysts and headlines
- [x] Renders now under `NEXT_PUBLIC_MARKET_MOCK=1`
- [ ] **backend:** implement this route by pulling `latest.json` (Market_News PR #40 publishes it) →
      map briefings to `top_movers` + `headlines` + `catalysts` → drop the flag → move the types from
      `lib/market.ts` into `lib/types.ts` and delete the fixture
