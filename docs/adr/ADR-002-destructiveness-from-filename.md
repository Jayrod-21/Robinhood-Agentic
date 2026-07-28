# ADR-002 — Migration destructiveness is declared in the filename, not the file

**Status:** accepted · **Date:** 2026-07-28

## Context

`db/migrate.py` gates data-destroying migrations behind `--allow-destructive`. The original design
classified a migration by reading a `-- migrate: destructive` / `-- migrate: non-destructive`
directive out of the SQL text, falling back to a keyword sniff. Deciding whether that directive was
"really a comment" required separating comments from string literals from dollar-quoted bodies from
code — i.e. reimplementing PostgreSQL's lexer (`scan.l`) in Python.

That approach failed three independent verification rounds in a row:

- **Round 1** (`docs/fixpass/REVIEW_migrate_runner.md`, B-1): four fail-open bypasses of the
  regex-based strippers — the worst forged a directive inside a `DO $$ … $$` body and applied a
  `DROP TABLE` without the flag.
- **Round 2** (`docs/fixpass/REVIEW_FIXES.md`, NEW-B1/NEW-S1): the rebuilt single-pass scanner was
  still forgeable via a non-ASCII dollar tag (`$café$`) — Postgres's tag alphabet is "any byte ≥
  0x80" — and via a directive trailing a blanked multi-line segment.
- **Round 3** (`docs/fixpass/REVIEW_FIXES_b1_residual.md`, NEW2-B1/NEW2-S1): **nine further
  forgeries**, seven applying a real `DROP TABLE`/`TRUNCATE` end-to-end with exit 0 — the directive
  regex matched across the comment/code boundary, and Python's notion of whitespace disagreed with
  Postgres's identifier alphabet (U+00A0 et al.). The same round also found a NUL byte silently
  truncating what libpq executes, and an O(n²) path in the scanner (73 s on 40 kB).

Round 3's closing diagnosis: every round fixed the *shapes* the previous reviewer produced, not the
*property* the gate needs. The classification signal lived inside the artifact an author (or a bug)
could manipulate, behind a lexer that had to match PostgreSQL's byte-for-byte, forever.

## Decision

**Delete the lexer. Move the classification signal to the filename.**

1. **Grammar:** `NNN_name.up.sql` / `NNN_name.down.sql`, with destructive migrations marked
   `NNN_name.destructive.up.sql` / `NNN_name.destructive.down.sql` — each direction marked
   independently. `FILENAME_RE` in `db/migrate.py` is the single source of truth; the name charset
   (`[a-z0-9_]`) excludes `.`, so the marker cannot be smuggled into or faked by a name, and any
   near-miss spelling fails discovery loudly — including a near-miss extension (`.SQL`, trailing
   dot/space/newline), which before round 5 was silently *skipped*, letting `up` report success
   while the migration never ran (R4-S2). A filename cannot be influenced by anything inside
   the file, so the entire forgery class is structurally gone — there is no parser left to attack.
2. **Best-effort sniff:** a file whose RAW text contains `DROP TABLE` / `DROP SCHEMA` /
   `DROP DATABASE` / `DROP OWNED` / `DROP MATERIALIZED` / `TRUNCATE` — keywords separated by
   whitespace or by SQL comments, which PostgreSQL's lexer treats as token separators
   (`drop/**/table` is a valid `DROP TABLE`) — without the filename marker is refused at
   discovery, with a message naming the exact rename. Deliberately no comment/literal stripping
   first: a false positive costs a rename (or a rewording). The sniff is a SECONDARY net and is
   deliberately incomplete: it has never covered mass `DELETE FROM` or `DROP COLUMN`, it does not
   see nested block-comment separators, and no text rule can decide dynamically built SQL —
   `EXECUTE 'DR'||'OP TABLE …'` destroys data without containing any keyword (round 4,
   `REVIEW_redesign_verification.md` R4-B1: seven such bodies applied unmarked with exit 0
   against the pre-round-5 whitespace-only sniff). The author marking the filename correctly is
   therefore the real control; the sniff only reduces the cost of forgetting it, for the common
   literal shapes.
3. **Transaction ownership is enforced by the server, not by parsing.** The old scanner's second
   consumer — rejecting top-level `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT` by reading the SQL — is
   replaced by a post-execution assertion: before the bookkeeping row is written, libpq must
   report `transaction_status == INTRANS` **and** `pg_catalog.pg_current_xact_id()` must equal the
   xid captured when the runner's transaction began. Verified live on PG16: a stray `COMMIT` or
   `ROLLBACK` leaves the connection IDLE (status check fires); `COMMIT; BEGIN;` restores INTRANS
   but changes the xid (xid check fires); a bare `BEGIN` or `SAVEPOINT` changes neither and is now
   tolerated — both are harmless to the body+bookkeeping atomicity the check protects (BEGIN
   inside a transaction is a server-side no-op warning). Detection is post-hoc by nature:
   statements a stray COMMIT already committed are durable, so the error says to inspect and clean
   up manually, and the migration is never recorded.
4. **Byte-level rejection at discovery:** NUL bytes (libpq C-string truncation → silent partial
   apply), a UTF-8 BOM, and invalid UTF-8 are refused before any analysis.

## Consequences

- The destructive CLASSIFICATION can no longer be forged from file contents — no attack on the
  filename channel has succeeded (round 4: 20/20 blocked). The round-1/2/3 forgery corpus is
  pinned end-to-end in `db/tests/test_runner_db.py` (`FORGERY_BODIES`): every shape is refused at
  discovery with the table intact. Note the layer doing the refusing: those bodies all carry a
  literal `DROP TABLE`, so it is the sniff that stops them (round 4 R4-S3). An unmarked file
  whose destructive statement the sniff cannot see (dynamic SQL) still applies — that reality is
  itself pinned in the test suite, and the filename marker is the control for it.
- `-- migrate:` directives are **dead**. The ones still present in the applied `001`–`003` up
  bodies are inert comments left in place deliberately: the up body is checksummed and editing an
  applied migration violates the checksum invariant. Down bodies are not checksummed, so the
  directive lines were removed from the renamed down files.
- The three down files were renamed to `*.destructive.down.sql`. This cannot orphan
  `schema_migrations` rows: the marker sits outside `FILENAME_RE`'s `name` capture group, so the
  recorded `version`/`name` are unchanged (verified live: `status` shows 001–003 applied,
  checksums `ok`, no ORPHAN rows).
- Discovery is linear again — regex search plus byte scans, no backscans. 3 MB of the old worst
  case discovers in ~0.01 s (the old scanner needed >30 s for the first 1.5 MB alone).
- Trade-offs accepted: `BEGIN`/`SAVEPOINT` in a body no longer error (they are provably harmless
  to atomicity); a hijacking `COMMIT` is detected after its damage rather than before (the old
  "before" was a forgeable text scan — a real detection late beats a forgeable rejection early);
  and prose like "DROP TABLE" in a comment of an unmarked file forces a rename or rewording.

ADR-001 (network isolation) is unaffected. The predecessor design and its verification history
remain in `docs/fixpass/` — this ADR supersedes the classification mechanism they describe.
