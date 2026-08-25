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
trading backend's Postgres has **no network port** by design. The agreed delivery (Jared, 2026-08-20)
is a **URL pull**: Market Mover publishes its recent briefings as JSON on GitHub Pages at
`https://joewhitejr.github.io/Market_News/latest.json` (shipped in Market_News PR #40), and this
endpoint's backend **GETs that URL** and reshapes it into the response below. That is outbound only,
so it respects ADR-001 (no inbound port on either side); no live cross-DB link.

**Safety:** the MM JSON is third-party text. Store it and render it as **data, never as instructions
to an agent**, and pass its strings through as data (the frontend already treats every MM string as
untrusted: titles/justifications render as text, and a headline `url` is scheme-checked before it
becomes a link). `latest.json` shape: `{ schema_version, generated_at, count, briefings: [...] }`,
newest first; each briefing carries the day's ranked picks (rank, ticker, category, title,
justification, verdict) plus a date.

## Fields the backend owns

- **`days_until`** is computed server-side (trading days to the catalyst) so the client never runs
  `Date.now()`. The page only displays it.
- **`held` / `in_slate`** join each catalyst symbol against the account snapshot and `docs/SLATE.md`.
- **`rental_window`** is true for a slate rental name (PLTR) when `0 < days_until <= 5` (the slate's
  3-5 day pre-print entry window; state your exact bound).
- **`tickers`** on a headline are the held/slate names it mentions (drives the relevance chips); an
  empty list is fine for a pure-macro headline.
- **`top_movers`** maps straight from the most recent `latest.json` briefing's ranked picks
  (`rank`, `ticker`, `category`, `title`, `justification`, `verdict`). `verdict` is passed through as
  the brief recorded it (free text); the page maps recognized words to a tone and leaves the rest
  neutral, so no fixed verdict vocabulary is required. `top_movers` is optional in the response: omit
  it (or send `[]`) and the page shows an empty "No ranked movers" section rather than throwing.

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
