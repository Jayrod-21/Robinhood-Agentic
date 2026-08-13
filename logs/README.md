# Logs — the append-only historical record

This is the project's audit trail: what happened, when, why. Separate from the *living* docs in
`docs/` (charter, theses, slate — which change in place). Logs are **append-only** — we add new
dated records, we don't rewrite old ones. This is the memory the eventual 24/7 app will consume.

> **Ownership note (2026-08-13).** Records dated before this entry were written during a
> single-operator build phase, and they read that way: they name one person as the approver, and
> the 2026-06-03 session log describes splitting this repo off to keep its strategy out of the
> shared 3a repo. The project is now co-owned, and that framing no longer describes intent.
>
> Those entries are deliberately **not** rewritten — the append-only rule exists precisely so the
> record stays trustworthy, and editing history to look better is the failure mode it guards
> against. Read them as an accurate account of what was true at the time, not as current policy.
> Current ownership and decision rights live in `docs/AGENTIC_ROBINHOOD_v1.md`.

## Structure

```
logs/
  sessions/   YYYY-MM-DD-session[-N].md   Human-readable narrative of a working session:
                                          timeline, user input/decisions, pushback, what changed.
  debates/    YYYY-MM-DD-debate-<slug>.md Full record of a multi-agent deliberation:
                                          question, positions, scores, verdict, synthesis.
  trades/     YYYY-MM-DD-execution.md     Order-by-order execution log: orders, blockers, fills,
                                          cost basis, resulting portfolio.
```

Naming: `YYYY-MM-DD-<slug>.md`, one record per event. Cross-link to the living docs
(`../docs/THESES.md`, `../docs/SLATE.md`) and to the journal (`../docs/agentic_journal.md`).

## Relationship to the other records
- **`docs/agentic_journal.md`** — the *running ledger* (positions, fills, scan log, lessons). The
  quick operational tape. Logs here are the *deep archive* (full debates, session narratives).
- **`docs/THESES.md` / `docs/SLATE.md`** — *current state* (overwritten as it evolves). Logs capture
  the *history* of how that state was reached.

## For the future 24/7 app (design note)
These markdown logs are the human-first version of what becomes the app's **event store / audit
trail**. When we build the local script/app:
- Mirror each event as an appended **JSONL** record (`logs/events.jsonl`) with a typed schema
  (`{ts, type: scan|debate|decision|order|fill|lesson, payload}`) so the app can replay/query it.
- Keep the markdown as the human-readable rendering; generate it from the JSONL, or write both.
- The daily-scan output, every order + fill, and every thesis change should emit an event.
- This log dir + the journal together are the seed of that event store — keep them faithful.
