# `GET /api/intraday` — the 30-minute ratio series

Issue #133. Frontend contract for the intraday observation log.

## Why this document exists

#135 built the tables, the arithmetic and the cron and stopped at the database. The series
accumulated with nothing able to read it — the frontend could not have rendered it even in
principle. This is the read path, and the shape the page should build against.

## Request

```
GET /api/intraday?symbol=NVDA&sessions=5&limit=2000
```

| Param | Default | Meaning |
|---|---|---|
| `symbol` | all | one security; case-insensitive |
| `sessions` | 1 | how many **trading sessions present in the table**, not calendar days — asking for 5 over a holiday week returns five sessions of data, not three |
| `limit` | 2000 | max points; `meta.truncated` says whether it clipped |

## Response

```jsonc
{
  "meta": {
    "sessions": ["2026-08-26"],
    "symbol": "NVDA",              // null when unfiltered
    "points": 15,
    "truncated": false,            // declared, never silent
    "last_run": {
      "started_at": "2026-08-26T16:38:23Z",
      "status": "complete",        // running | complete | failed | skipped
      "scope_size": 15, "observed": 15, "failed": 0,
      "error": null                // present for skipped/failed — usually "market closed"
    }
  },
  "observations": [
    {
      "symbol": "AMD",
      "observed_at": "2026-08-26T16:38:24Z",
      "session_date": "2026-08-26",
      "price": 481.87,
      "market_cap": 786519910000.0,
      "volume": 4823666,
      "pe_trailing": null,
      "pe_forward": null,
      "fcf_yield": 0.008572,
      "scope_reasons": ["debated", "held"],
      "has_lineage": true,
      "formula_version": 1
    }
  ]
}
```

## Three things the page must not flatten

**1. `last_run` is why an empty chart is empty.** Without it, no data is ambiguous between *the
market is closed*, *the collector died*, and *this symbol left the watchlist*. A `skipped` run with
`error: "market closed…"` is the normal overnight state and must not render as a fault.

**2. `has_lineage` explains a null ratio.** A null **with** lineage means the filing in effect did
not carry that figure. A null **without** lineage means there was no filing to read at all. Those
are different, and the page can say which.

**3. `pe_forward` is null on every row today**, because `eps_next_year_est` is populated on 0 of 152
`fundamentals_snapshots` rows. That is a loader gap, not a rendering bug — do not build a chart that
looks broken when it is honestly empty. `pe_trailing` is null wherever the in-effect filing carried
no EPS (89 of 152 rows have one).

## Values are recorded, not interpreted

A **negative** `fcf_yield` means the company burns cash — QBTS and SVRA both do. It is stored signed
rather than nulled, because a consumer reading null cannot tell *"burns cash"* from *"we had no
figure"*. Same for a negative `pe_trailing` when EPS is negative. If the page wants to render those
as "n/a", that is the page's decision to make and to label.

## `formula_version`

Which arithmetic produced the row. It exists so a corrected formula can be applied retroactively —
`pe_forward` was once mapped from a PEG ratio, and without this column every row computed under
that mapping would have been permanently indistinguishable from a correct one. The page can ignore
it; a mixed-version chart is worth a note.

## What this endpoint will never do

Compute a ratio. Everything is read from `intraday_observations` exactly as the collector stored it.
If a ratio is wrong it is wrong in the table, and the fix is a recompute — not a second
implementation of the arithmetic in a router, which is how the two come to disagree.
