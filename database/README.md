# Market Data Pipeline — Database

> **⚠ Status, 2026-08-26: this directory is a DESIGN, not the live schema.** None of the tables
> below exist. The live database is built by `db/migrations/` through `db/migrate.py` (25 migrations
> as of today) — `001_schema.sql`, `002_views.sql` and `003_seed.sql` here have never been applied.
>
> The `market_mover_emails` subtree below is **retired**. Market Mover now publishes its brief as
> JSON (`backend/scripts/sync_market_mover_brief.py` pulls it into `data/market_mover/latest.json`),
> so there is no email to parse and no reason to store one. The email-ingest code that existed for
> it was removed on 2026-08-26.
>
> The `equity_snapshots` / `equity_fundamentals` / `equity_analyst_snapshots` tiering, on the other
> hand, is **live prior art** for issue #133 (the intraday ratio log): the FK-encoded design where a
> snapshot points at the Tier 2/3 rows its ratios were computed from, and no derived value is
> duplicated, is exactly the shape that issue calls for. Read this before designing that.

## Migrations

Run in order:

```bash
psql $DATABASE_URL -f 001_schema.sql
psql $DATABASE_URL -f 002_views.sql
psql $DATABASE_URL -f 003_seed.sql
```

## Schema Overview

### Data model

```
tickers (master symbol list)
│
├── equity_fundamentals      Tier 3 — quarterly financial statements
│                            One row per (ticker, fiscal quarter end).
│                            Forward-fill = read the latest row.
│
├── equity_analyst_snapshots Tier 2 — weekly analyst estimates + short interest
│                            One row per (ticker, fetch date).
│
└── equity_snapshots         Tier 1 — 3× daily price + price-derived ratios
                             FKs: fundamentals_id → equity_fundamentals
                                  analyst_snapshot_id → equity_analyst_snapshots
                             The FK columns encode which Tier 2/3 rows were used
                             to compute the ratios — no values are duplicated.

market_mover_emails
│
├── news_stories             Top stories per email
│   ├── news_story_tickers   Junction: (story → symbol) — no FK to tickers,
│   │                        news can mention symbols outside our watchlist
│   └── news_story_sectors   Junction: (story → sector)
│
├── news_bear_cases          Bear case section per email
│   └── news_bear_case_tickers Junction: (bear_case → symbol)
│
├── news_scorecard           Grading of previous-day predictions
│   └── original_story_id   → news_stories (nullable: story may predate DB)
│
└── earnings_from_news       Earnings calendar extracted from email
    └── ticker_id            → tickers (nullable: symbol may not be in watchlist)
```

### Normalization decisions

| Decision | Reason |
|---|---|
| `symbol` only in `tickers` | Single source of truth. All other tables use `ticker_id` FK. |
| Three separate data-tier tables | Quarterly data stored once, not repeated in every daily snapshot row. |
| `equity_snapshots.fundamentals_id` FK | Full lineage: know exactly which quarterly row each ratio was computed against. |
| Junction tables for news tickers/sectors | Proper many-to-many; allows GIN-indexed symbol lookups. |
| `news_story_tickers.symbol` has no FK to `tickers` | Intentional — news can reference symbols we don't track. |
| `earnings_from_news.ticker_id` nullable | Company may not be in our watchlist. `symbol` stored as fallback. |
| `news_scorecard.original_story_id` nullable | Story being graded may predate our DB. |

### Views

| View | Purpose |
|---|---|
| `v_equity_full` | All 50 Bloomberg fields per snapshot (joins all three tiers) |
| `v_equity_latest` | Most recent snapshot per ticker — use for dashboards and scan input |
| `v_news_today` | Today's stories with tickers/sectors aggregated — feed into debate context |
| `v_news_for_ticker` | All news for a symbol across any date range |

### Forward-fill pattern

Quarterly data does not change between earnings releases. The application layer sets
`fundamentals_id` to the latest `equity_fundamentals` row for the ticker on every
snapshot insert. If no new quarterly data has been pulled, the FK points to the same
row it did last week — that IS the forward-fill. No NULL columns, no copied values.

```sql
-- Get the full picture for AMD at its latest snapshot:
SELECT * FROM v_equity_latest WHERE symbol = 'AMD';

-- Get all AMD snapshots in the last 30 days:
SELECT s.snapshot_time, s.session, s.price, s.pe_trailing, f.gross_margin
FROM equity_snapshots s
JOIN tickers t ON t.id = s.ticker_id
LEFT JOIN equity_fundamentals f ON f.id = s.fundamentals_id
WHERE t.symbol = 'AMD'
  AND s.snapshot_time >= NOW() - INTERVAL '30 days'
ORDER BY s.snapshot_time DESC;
```
