# Phase 4: data export + live debate viewer (status)

This is the lightest phase, and most of it needed no new backend, so this is a status note rather than
a contract with pending work.

## Debate transcript export: shipped, client-side, no backend

The debate detail page (`frontend/src/app/debate/[id]/page.tsx`) already fetches the full record from
`/api/debate/{id}`, so exporting it is a pure browser operation. `frontend/src/components/debate-export.tsx`
adds **JSON** and **Markdown** download buttons to the detail header, using `frontend/src/lib/export.ts`
(`downloadText()` via Blob + object URL; `debateToMarkdown()` reconstructs a readable transcript from the
structured record, or uses an archive record's own `markdown`). Nothing to implement on the backend.

## Live debate viewer: already exists

The Debate page (`frontend/src/app/debate/page.tsx`) already streams a live run via
`streamSSE("/api/debate/run-stream", ...)` with a "Run debate" button and live bull/bear/jury/decision
updates. This is what Jared stood up. No new work; the plan's "wire a live view" item is done.

## Optional future work (not needed for the stated goal)

- **Bulk / whole-corpus export:** a server-side `GET /api/export/debates?format=csv|jsonl` streaming all
  records at once (from the already-persisted `logs/debates/*.json`), for a full dump rather than one
  transcript at a time. Client-side covers the per-debate need; this is only if a bulk export is wanted.
- **CSV of the records list:** if a debates-list/records table is added, a client-side CSV export can be
  built the same way as the transcript export (no backend).

## Done

- [x] JSON + Markdown export on the debate detail page (client-side)
- [x] Confirmed the live debate viewer already exists (no duplication)
- [ ] optional: server-side bulk export (only if a whole-corpus dump is wanted)
