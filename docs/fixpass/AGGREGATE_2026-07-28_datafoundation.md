# Aggregate — /fixpass on the 3b data foundation (2026-07-28)

Phase 2 of the cycle: combining three independent reviews before the fix-pass.

**Scope reviewed:** `db/migrate.py`, `db/migrations/001-003`, `docker-compose.db.yml`,
`bin/db_up.sh`, `bin/db_psql.sh`, `bin/db_migrate.sh`, `bin/lib_ports.sh`, `db/Dockerfile`,
`db/.env.example`.

**Quality contract:** `SENIOR_ENGINEER_BAR.md` (symlinked at the project root).
**Pre-existing decisions:** `docs/adr/ADR-001-db-network-isolation.md`.

## Verdicts

| Reviewer scope | Verdict | BLOCKER | SHOULD-FIX | Report |
|---|---|---|---|---|
| Migration runner | REQUEST CHANGES | 2 | 6 | `REVIEW_migrate_runner.md` |
| Schema (001-003) | REQUEST CHANGES | 2 | 8 | `REVIEW_schema.md` |
| Infra & security posture | REQUEST CHANGES | 5 | 11 | `REVIEW_db_infra.md` |
| **Total** | — | **9** | **25** | |

## Blockers

| ID | Finding |
|---|---|
| Runner B-1 | SQL stripping layer fails open — four demonstrated bypasses. A `-- migrate: non-destructive` directive inside a `DO $$ … $$` body is honored, letting a `DROP TABLE` migration bypass `--allow-destructive`. |
| Runner B-2 | Zero tests. No `db/tests/` exists. |
| Schema B1 | `uq_fundamentals_snapshot` omits `known_at` — restatements unstorable, and a natural `ON CONFLICT DO UPDATE` silently replaces as-first-reported fundamentals with as-restated ones. A lookahead enabler in the table designed to prevent lookahead. Corollary: `DELETE FROM data_sources` fails inside its own `SET NULL` cascade. |
| Schema B2 | DEFAULT partition plus a single-month `ensure_price_bar_partition()` deterministically wedges ingest at the first EST month-end (2020-11-30): post-market EST bars land past UTC midnight in DEFAULT, after which the next month's partition can never be attached. |
| Infra B1 | `bin/db_up.sh:44` places the generated Postgres password in `awk`'s argv — proved via `/proc/PID/cmdline`. The file's own comment claims the opposite. |
| Infra B2 | `db/.env` created at 0664 and only `chmod 600`'d afterwards — a real window. |
| Infra B3 | `bin/lib_ports.sh` returns a false "free" verdict for 9b's live ports 1840-1841: it cannot parse published port RANGES, and `docker ps` without `-a` cannot see stopped containers — which is the check's entire stated purpose. **A live hazard to another production stack on this machine.** |
| Infra B4 | `db/.env.example` rotation instructions rotate nothing — Postgres reads `POSTGRES_PASSWORD` only on first init. |
| Infra B5 | ADR-001's isolation claim is false: the host reaches the database directly on the bridge IP. |

## Cross-cutting

**Every infra blocker is a comment asserting a security property that does not survive testing.** This
is the same pattern the project had just documented finding in a sibling repo (9b's `km-db` declares a
host port binding that an internal-only network silently discards), and then reproduced four times in
new code. The lesson generalizes past this cycle: a comment claiming a safety property is worse than
no comment, because it stops the next reader from checking.

Two blockers compound: B1 (password in argv) was written as defence-in-depth *behind* the network
isolation that B5 shows does not hold. With the isolation claim false, that password is the actual
on-box boundary.

## PRAISE — protected from the fix-pass

- The transaction atomicity guarantee — independently verified correct against real psycopg3
  behaviour (`autocommit=True` + `conn.transaction()` rolls back body and bookkeeping together).
- The point-in-time design: the `period_end` / `known_at` split, the provenance model, delisted
  retention, and the `unparsed` JSONB preserving Excel error strings verbatim.
- The egress block — verified genuinely real, including the DNS channel.
- Image digests verified against Docker Hub; `deploy.resources.limits` empirically honoured rather
  than silently ignored.
- `bin/db_psql.sh`'s `sh -c '…"$@"' -- "$@"` proved injection-safe against `$(id)`, backticks, and
  `;rm -rf /`.

## Outcome

Fix-pass dispatched on all 9 BLOCKERs and all 25 SHOULD-FIXes. See `FIX_REPORT.md`, then
`REVIEW_FIXES.md` (re-review), `REVIEW_FIXES_b1_residual.md` (verification of the residual B-1 fix),
`docs/adr/ADR-002-destructiveness-from-filename.md` (the redesign that followed), and
`REVIEW_redesign_verification.md`.

**Note on B-1's trajectory:** it took three verification rounds and a redesign. Content-based
classification required reimplementing PostgreSQL's lexer, and each round closed only the bypasses
someone thought to try — thirteen forgeries in total. The gate now reads the filename, which nothing
inside the file can influence. That is recorded in ADR-002 so the approach is not rebuilt.
