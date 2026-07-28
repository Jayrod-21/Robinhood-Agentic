# Fix-pass report — migration runner · schema 001-003 · DB infrastructure

**Fixer:** independent fix-pass (did not write or review this code) · **Date:** 2026-07-28
**Scope:** every BLOCKER and SHOULD-FIX in `REVIEW_migrate_runner.md`, `REVIEW_schema.md`,
`REVIEW_db_infra.md`; NITs where trivially fixable in files already being touched.
*(The previous contents of this file — the June dashboard fix-pass — are preserved in git history.)*

## Approach for the schema fixes

Migrations 001-003 were **edited in place**, then the live `rh-db` was cycled
`down --allow-destructive --target 000` → `up`. Rationale: the schema has never shipped and the
database held no data (verified: only `schema_migrations` rows). Stacking corrective migrations on
a never-shipped schema would permanently encode the reviewers' findings into the migration history.
The rollback used the **pre-fixpass down files extracted from git** (`f0e49ee`), since those match
what was actually applied — running the new down files against the old schema would have left the
old single-month `ensure_price_bar_partition(DATE)` behind as residue. Verified zero residue
between down and up (1 table = bookkeeping only, 0 public functions). A verified `pg_dump` backup
was taken before the cycle (`data/backups/db/rh_db_20260728_163815.dump`).

Every claim marked *(verified)* below was tested empirically on this box, not asserted.

---

## Dispositions — REVIEW_migrate_runner.md

| ID | Severity | Disposition | Note |
|---|---|---|---|
| B-1 | BLOCKER | **FIXED** | Regex passes replaced by one left-to-right scanner (`_scan_sql`) tracking code / line-comment / nested-block-comment / `'…'` / `E'…'` / `"…"` / `$tag$…$tag$` state; all three strip views derive from it. All four fail-open cases pinned as regression tests (`db/tests/test_sql_scan.py`), plus an end-to-end test proving a forged directive in a dollar-quoted body no longer bypasses `--allow-destructive` (`test_runner_db.py::test_b1_regression_forged_directive_does_not_bypass_gate`). Unterminated tokens now raise `SqlLexError` at discovery (fail loud, not misclassify). Directives are honored **only** in `--` line comments on their own line — a stricter, documented contract. |
| B-2 | BLOCKER | **FIXED** | `db/tests/` created with the three-layer model: 24 scanner/gate unit tests, 22 discovery/`--target`/CLI-contract tests, 9 testcontainers-backed integration tests including the ACTUAL 001-003 up→down→up cycle with schema-behavior assertions. 64 tests total, registered in `TESTS.md` (§3b) and `pyproject.toml` testpaths. Every blocker fix has a test that fails on the old code. |
| SF-1 | SHOULD-FIX | **FIXED** | `validate_target()` requires membership in the discovered version set (down additionally allows the all-zeros sentinel), runs after discovery and **before** connecting; unpadded `--target 2` now exits 1 with the valid set named *(tested without a DB, proving order)*. |
| SF-2 | SHOULD-FIX | **FIXED** | `SELECT pg_advisory_lock(MIGRATION_LOCK_KEY)` after session setup — blocking, session-scoped, released on disconnect. Constant documented as lock identity. |
| SF-3 | SHOULD-FIX | **FIXED** | `connect_timeout` (default 10 s, `MIGRATE_CONNECT_TIMEOUT` override, integer-validated); the `SET`/lock block is wrapped in `try/except psycopg.Error: conn.close(); raise`. |
| SF-4 | SHOULD-FIX | **FIXED** | `up_sql`/`down_sql`/`checksum` are read once at discovery and stored on the frozen dataclass; validated text ≡ executed text structurally. Regression test edits the file post-discovery and asserts the cached text is used. |
| SF-5 | SHOULD-FIX | **FIXED** | Discovery rejects mixed version widths with the widening procedure in the message *(tested: 999 + 1000)*. |
| SF-6 | SHOULD-FIX | **FIXED** | `_Parser.error()` exits `EXIT_VALIDATION` (1); `--help` still 0; `main()` returns rather than raising, so tests call it as a function *(tested)*. Exit-code doc updated. |
| N-1 | NIT | **FIXED** | Redundant `finally: conn.close()` removed; comment states `with conn:` closes on both paths and releases the advisory lock. |
| N-2 | NIT | **FIXED** | `--dry-run` help now says it still creates the bookkeeping table if absent — claim matches behavior. |
| N-3 | NIT | **FIXED** | By the scanner: nested block comments and `"begin"` quoted identifiers no longer false-positive the tx-control check *(both in the no-false-positive test table)*. |
| N-4 | NIT | **FIXED** | `DIRECTIVE_RE` comment documents line-comment-only + own-line recognition; `/* migrate: … */` is now *provably* inert (test). |
| N-5 | NIT | **DEFERRED** | Informational `down_checksum` requires evolving the `schema_migrations` table on existing databases — `CREATE TABLE IF NOT EXISTS` won't add a column, so it needs a small internal-migration step done deliberately, not smuggled into a fix-pass. Follow-up item; no correctness impact (the rationale documented at the checksum field stands). |
| N-6 | NIT | **FIXED** | `_warn_orphans()` logs applied-but-fileless versions on `up` and `down`, not only `status`. |
| N-7 | NIT | **FIXED** | Module docstring gained a LIMITATION section: `CREATE INDEX CONCURRENTLY`/`VACUUM` cannot run in a migration; needs an out-of-band path. |
| P-1…P-5 | PRAISE | **PRESERVED** | `autocommit=True` + `conn.transaction()` untouched (atomicity re-verified by `test_failed_migration_rolls_back_body_and_bookkeeping_together`); checksum-before-plan, discovery-time validation, plan-time destructive gate incl. `--dry-run`, `clock_timestamp()` duration, parameterized bookkeeping — all intact and now under test. |

## Dispositions — REVIEW_schema.md

| ID | Severity | Disposition | Note |
|---|---|---|---|
| B1 | BLOCKER | **FIXED** | `uq_fundamentals_snapshot` is now `(security_id, period_end, period_type, source_id, known_at) NULLS NOT DISTINCT` (PG15+; verified in live catalog). Rows are append-only observations: as-first-reported and as-restated coexist *(verified: two rows, same period, different known_at)*; identical observations dedupe *(verified: unique violation)*; two NULL-source NULL-known_at loads collide instead of silently duplicating *(verified)*. Loader contract documented in the migration: `ON CONFLICT DO NOTHING` is idempotent re-ingest; there is **no legitimate `DO UPDATE`**. The COALESCE hack is gone. Corollaries: `source_id` FK is now `ON DELETE RESTRICT` (named `fk_fundamentals_source`), so the SET-NULL-cascade unique collision cannot occur and `DELETE FROM data_sources` on a referenced row is refused *(verified: FK violation)*. |
| B2 | BLOCKER | **FIXED** | Deliberate decision: **DEFAULT partition dropped — out-of-range inserts fail loudly** *(verified: "no partition of relation" error)*, so ingest can never wedge on attach. Helper replaced by `ensure_price_bar_partitions(p_from, p_to)` covering the file's actual ts range, with reversed-range and ≥240-month garbage-timestamp guards *(all verified)*; contract comment names the EST-spillover mechanism and the Nov 30 2020 case. The full archive window 2020-10…2025-11 (62 partitions, incl. one month headroom past the 5-year set's 2025-10-02 end) is pre-created in the migration *(verified: 62 in live catalog; spillover bar at 2020-12-01 00:30 UTC lands in `price_bars_minute_2020_12`)*. |
| F1 | SHOULD-FIX | **FIXED** | `fundamentals_asof(security_id, asof, period_type DEFAULT NULL)` — SQL/STABLE, pins the `known_at` filter, rides `ix_fundamentals_pit`, excludes NULL `known_at` by construction *(verified: returns pre-restatement value before its known_at, restated value after, nothing before either)*. Comment states the review discipline: backtests read the accessor, never the raw table. `period_type` parameter added beyond the review's sketch so consumers needn't fall back to raw queries when a quarterly would shadow an annual. |
| F2 | SHOULD-FIX | **FIXED** | Policy decided and documented: canonical form = Polygon flat-file ticker **verbatim**; other providers normalize to it; skipping is a logged loader decision. CHECK widened to `^[A-Za-z][A-Za-z0-9]{0,9}(\.[A-Za-z0-9]{1,4}){0,2}$`, validated against three real day files across the archive (2020-11-30, 2021-06-30, 2023-06-30): 12,928 distinct tickers, **0 rejected** (the old regex rejected 482 in the first file alone). Evidence recorded in the migration comment. *(Live-verified: `BACpA`, `TDW.WS.A`, `AANw` accepted; junk rejected.)* |
| F3 | SHOULD-FIX | **FIXED** | Global unique replaced by partial `uq_securities_symbol_live … WHERE delisted_at IS NULL` + plain `ix_securities_symbol`; loader rule (recycled ticker = new row; historical resolution via symbol + as-of date) documented. *(Verified: second live holder refused; delisted + new live holder coexist.)* |
| F4 | SHOULD-FIX | **FIXED** | Per the reviewer's own recommendation, **not** four blind indexes: all `source_id` FKs → `ON DELETE RESTRICT`; `data_sources` documented append-only (001 header); indexed **only** on `fundamentals_snapshots` (`ix_fundamentals_source`, the audit query); the deliberate §4.1 deviation is written into both 001 and 002 where the unindexed FKs live. |
| F5 | SHOULD-FIX | **FIXED** | `is_active` **dropped** (the reviewer's stronger option): active ≡ `delisted_at IS NULL`; one source of truth; indexes/comments adjusted. |
| F6 | SHOULD-FIX | **FIXED** | Every FK explicitly named `fk_…` *(verified in live catalog: 7 distinct names; the minute-table ones propagate per partition, as Postgres does)*. |
| F7 | SHOULD-FIX | **FIXED** | `data_sources` + `price_bars_daily` get `updated_at` + trigger (mutable `row_count`/`notes`; late-arriving `adj_close`); the minute-table omission is now a documented deviation with the ~26 GB cost stated, inside the PK comment where a reader will meet it. |
| F8 | SHOULD-FIX | **FIXED** | `ix_fundamentals_screen` removed; a NOTE in 003 records why and the `EXPLAIN (ANALYZE, BUFFERS)`-against-real-data bar any successor must clear. |
| N1 | NIT | **FIXED** | `known_at >= (period_end::timestamp AT TIME ZONE 'UTC')` — session-TimeZone-independent *(violation verified in the test cycle)*. |
| N2 | NIT | **FIXED** | Helper schema-qualifies both the `to_regclass` check and the `CREATE TABLE public.%I … PARTITION OF public.price_bars_minute`. |
| N3 | NIT | **FIXED** | Redundant `high >= low` CHECKs removed on both bar tables; the `_ohlc` comment records the implication so nobody re-adds it. |
| N4 | NIT | **FIXED** | `first_seen` NULL convention documented in-column and via `COMMENT ON COLUMN`. |
| Coordination | — | noted | The two EVALUATION_FRAMEWORK §4 hard constraints (`n_observations NOT NULL`; as-of timestamps on metric rows) bind on the future `evaluation_runs` migration — carried here as an explicit acceptance criterion for 004+. Nothing in the fixed 001-003 conflicts with the §4 table plan. |
| P1…P5 | PRAISE | **PRESERVED** | `period_end`/`known_at` split (strengthened by B1, not simplified), `unparsed` JSONB, `data_sources` provenance/`fetched_at`/sha-dedup, delisted retention (strengthened by F3/F5), honest destructive down annotations — all intact. |

## Dispositions — REVIEW_db_infra.md

| ID | Severity | Disposition | Note |
|---|---|---|---|
| B1 | BLOCKER | **FIXED** | Reviewer's option (b): one python process generates the password and writes `db/.env` — the secret exists in exactly one process and **never in any argv** (the awk pipeline is gone). Comment now claims only what holds. |
| B2 | BLOCKER | **FIXED** | Same python one-shot opens with `O_CREAT|O_EXCL, 0o600` — no loose-mode window, no overwrite, and an interrupt cannot strand a world-readable file *(verified on a scratch copy: mode 600 at creation; O_EXCL refuses an existing file)*. |
| B3 | BLOCKER | **FIXED (urgent item)** | `docker_published_ports` reads `HostConfig.PortBindings` via `docker ps -aq \| docker inspect` — covers **stopped** containers and expands **ranges** (both the rendered form and range-valued `HostPort`). *(Verified live: 1840, 1841, 1842, 1843 all report BUSY; a stopped throwaway container's port reports BUSY; a stopped published **range** reports BUSY on both ends.)* Header comment updated to the now-true claim. |
| B4 | BLOCKER | **FIXED** | `db/.env.example` documents the real two-step rotation — `\password rh` (client-side hash: the new secret never appears in argv or server logs) then edit the file — and states explicitly that `POSTGRES_PASSWORD` is initdb-only and recreation rotates nothing. |
| B5 | BLOCKER | **FIXED** | Compose header rewritten to the verified truth: no host port; on-box reachable via the unstable bridge IP behind scram (the password is the on-box control — equivalent to loopback + 0600 file, **not stronger**); off-box/cross-bridge closed; egress incl. DNS blocked. Dangling `DB_PORT`/`.env.db`/`pick_db_port.sh` references deleted here, in `db/.env.example`, and in `bin/pick_ports.sh`. **ADR-001 corrected** with an amendment note: the "stronger than loopback" Good bullet replaced by a "What this does NOT provide" section; the false "venv cannot open a socket" Bad bullet corrected to instability-not-impossibility. The real egress property is stated verbatim, not weakened. |
| S1 | SHOULD-FIX | **FIXED** | No URL is built at all: `db_migrate.sh` exports `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE` and passes them by name; `connect_from_env()` accepts libpq-env configuration (empty DSN) when `DATABASE_URL` is unset — the cross-cutting change the reviewer flagged, both files owned by this pass. `DATABASE_URL` still works (tests use it). |
| S2 | SHOULD-FIX | **FIXED** | No `source db/.env` anywhere: compose consumes it via `--env-file` (a data parser, not a shell); `db_migrate.sh` uses a grep-based `read_env_value`; `db_up.sh` validates keys by grep. The `${VAR:?}` guards the reviewer praised are preserved in compose. |
| S3 | SHOULD-FIX | **FIXED** | Server defaults now `statement_timeout=60s`, `idle_in_transaction_session_timeout=300s` *(verified via SHOW and the live postmaster argv)*; the comment describes exactly this design; migrate.py's session-scoped opt-out is cross-referenced from both files; the loader's `SET LOCAL` obligation stated. |
| S4 | SHOULD-FIX | **FIXED** | Healthcheck is an authenticated `psql … -c 'SELECT 1'` against 127.0.0.1 — proves postmaster + role + database, and keeps BOTH properties the review said to preserve: the P10 IPv4 rationale and the initdb-window fail-closed behavior (the temporary initdb server is socket-only, so a TCP probe stays unhealthy during init — documented in the comment) *(healthy on the live container)*. |
| S5 | SHOULD-FIX | **FIXED** | `.github/workflows/image-scan.yml`: builds `rh-migrate`, Trivy-gates it and the pinned postgres digest at HIGH/CRITICAL-with-fix, weekly rescan cron, CycloneDX SBOM artifact; actions pinned to commit SHAs. Validated locally: rh-migrate scans clean; the postgres base gate is scoped past `gosu` (upstream's **current** image — verified identical to our pin — ships gosu on a stale Go stdlib; its 15 findings are unreachable in a one-shot privilege-drop binary that never touches a socket; the scope is documented in-workflow with a re-test obligation on digest bumps). A permanently-red gate on unactionable findings teaches people to ignore it. |
| S6 | SHOULD-FIX | **FIXED** | `psycopg[binary]==3.3.4` (current), installed `--require-hashes` from `db/requirements.txt` with sha256 pins for psycopg, psycopg-binary (cp312/manylinux), and typing-extensions; regeneration procedure documented in the file. |
| S7 | SHOULD-FIX | **FIXED** | `db/.dockerignore` added (`.env*` except example, `__pycache__`, `tests/`), with the context-relative gotcha explained in-file. |
| S8 | SHOULD-FIX | **FIXED** | `read_only: true` + two tmpfs mounts (socket dir uid 70, `/tmp`), `pids: 512` (under `deploy.resources.limits` — compose rejects the standalone key next to `deploy`), `memswap_limit = mem` *(all verified on the recreated live container: ReadonlyRootfs=true, MemorySwap==Memory=2 GiB, PidsLimit=512; `touch /etc/probe` refused)*. |
| S9 | SHOULD-FIX | **FIXED** | Migration 001 provisions `rh_app`: LOGIN, no password (cannot authenticate until an operator sets one — shipping it adds no surface), DML-only via `ALTER DEFAULT PRIVILEGES` so 002+ and future tables inherit grants with no boilerplate; `schema_migrations` deliberately not granted. Down revokes via `DROP OWNED` and drops the role *(verified in the test cycle: exists, not superuser; gone after down)*. |
| S10 | SHOULD-FIX | **FIXED** | `bin/db_backup.sh`: custom-format `pg_dump` via docker exec → `data/backups/db/` (0700), **verified restorable-shaped** via `pg_restore --list` before the `.partial` → final rename, retention 14. *(Ran live: dump verified.)* Volume protected structurally: `external: true` in compose (a `down -v` can no longer delete it) + idempotent `docker volume create` in `db_up.sh`. Scheduling is operator cron by design, stated in the header; off-host copies explicitly called out as not provided. |
| S11 | SHOULD-FIX | **FIXED** | Build inputs (Dockerfile + requirements.txt) hashed into an image label; a mismatch rebuilds. A bumped pin can no longer be silently ignored *(exercised live: the runner image rebuilt on first post-fix invocation)*. |
| N1 | NIT | **FIXED** | Bounds travel as argv to python and are validated; `PORT_MAX < PORT_MIN` yields a one-line message, not a traceback *(verified)*. |
| N2 | NIT | **FIXED** | `pick_free_port` reads all arguments (`$*`) as its doc line always claimed. |
| N3 | NIT | **FIXED** | Header no longer calls the bind probe "authoritative"; the IPv6/UDP blind spots and the combined-verdict reasoning are stated. |
| N4 | NIT | **FIXED** | `docker exec --user postgres`; PGPASSWORD retained with a truthful comment (trust on in-container loopback today; kept so a future hardened pg_hba doesn't break the wrapper). |
| N5 | NIT | **FIXED** | `RH_DB_CONTAINER` override (also honored by `db_backup.sh`). |
| N6 | NIT | **FIXED** | `stop_grace_period: 60s` *(verified: StopTimeout=60)*. |
| N7 | NIT | **FIXED** | Credentials validated before the build step. |
| N8 | NIT | **REJECTED (kept as-is)** | The shebang stays: the review itself notes it buys editor/shellcheck detection and the execution guard makes it harmless. Removing it costs tooling for zero gain. |
| N9 | NIT | **FIXED** | `db/__pycache__/` removed; `db/.dockerignore` and the root `.gitignore` both exclude it going forward. |
| P1…P10 | PRAISE | **PRESERVED** | `db_psql.sh`'s injection-safe `sh -c '…"$@"' -- "$@"` untouched; the bind probe still omits `SO_REUSEADDR`; the ss parser untouched; both image digests unchanged; deploy limits kept; the egress block untouched; fail-closed paths kept (and extended); the cap list unchanged; namespace hygiene unchanged (no `down`/`prune`/`rm` added anywhere); the 127.0.0.1 healthcheck rationale kept verbatim. |

---

## Gate outputs (all green)

```
$ .venv/bin/ruff check backend/app src db
All checks passed!

$ .venv/bin/python -m pytest -q
151 passed in 7.20s        # 87 pre-existing + 64 new (db/tests: 24 scanner, 22 discovery/CLI,
                           # 9 testcontainers integration incl. real 001-003 up→down→up)

$ shellcheck -x bin/*.sh
(clean, rc=0)
```

Live state at hand-off: `rh-db` running and **healthy** under the hardened config (read-only
rootfs, memswap=mem, pids 512, authenticated healthcheck, 60s/300s server timeouts);
`bin/db_migrate.sh status` → 001/002/003 `applied` / checksum `ok`; volume `rh_db_data` intact and
now `external: true`; one verified backup in `data/backups/db/`. No `km-*` container, volume, or
network was touched at any point (the only containers created were two throwaway `rhfix-*` port
probes plus testcontainers' ephemeral postgres, all removed). `data/market/` untouched beyond
read-only sampling of three day files for the F2 evidence.

## Self-assessment against the bar's "done" checklist

- **§0 fail closed/loud** — both text-analysis gates now fail closed by construction (scanner +
  `SqlLexError` on invalid SQL); no-DEFAULT partitioning fails loud; every silent no-op the
  reviews found (`down --target`, rotation, port checks) now errors or warns.
- **§0 comments true** — the cross-cutting theme. Every comment touched was re-verified against
  behavior on this box before being written: argv/mode claims, healthcheck semantics, timeout
  inversion, reachability, docker-ports parsing, dry-run claim, sniff asymmetry.
- **§1 Python** — full type hints maintained, no `Any`, narrow excepts (the one annotated
  `noqa: BLE001` is pre-existing and justified); an explicit timeout on the one outbound call.
- **§3 security** — secret never in argv, 0600 at creation, no shell-sourcing of data files, no
  credential URL assembly, hash-pinned deps, `.dockerignore` for the secret-bearing context,
  SHA-pinned actions, image scanning + SBOM, least-privilege `rh_app` provisioned.
- **§4 database** — restatement-correct uniqueness, named FKs with explicit `ON DELETE`,
  documented index deviations, UTC-anchored CHECK, both directions of all migrations tested
  against a real PG16, advisory-lock serialization, server-side timeouts set.
- **§5 testing** — behavior-asserting tests at three layers, a regression test per blocker
  (§5.2 [P0]), real owned infra via testcontainers, registered in TESTS.md.
- **§6 containers** — read-only rootfs, tmpfs, pids/memswap caps, stop grace, external volume,
  verified backups.
- **§7.2 live-money** — the destructive gate is now actually unforgeable within its trust model,
  loud, and overridable (`--allow-destructive`); point-in-time correctness is enforced by an
  accessor, not a comment; survivorship/ticker-reuse handling strengthened; the 5-year load can
  no longer wedge at an EST month boundary.

Known deliberate leftovers: N-5 (informational down-checksum) deferred with rationale above;
backup scheduling is operator cron by design; the `EVALUATION_FRAMEWORK` §4 constraints are
recorded here as acceptance criteria for the future 004+ evaluation-tables migration.

---

## Follow-up fix-pass — B-1 residual (D1, D2)

**Fixer:** independent (did not write the runner, the reviews, or the previous fix-pass) ·
**Date:** 2026-07-28 · **Scope:** the re-review's NEW-B1 and NEW-S1 only — `db/migrate.py`
scanner + `db/tests/`. Nothing else touched. The FIXED verdict on B-1 above was wrong; with this
pass and the re-review's evidence, B-1 is now closed for every shape either reviewer produced.

Every defect below was **reproduced against the pre-fix code first** (all five re-review shapes
confirmed byte-for-byte), and every grammar claim in the new comments was **executed against the
live PG16** (`bin/db_psql.sh`, read-only SELECTs): `$€$`, `$٣$`, `$t1$` accepted as tags;
`a$b$c` returns as ONE identifier; `SELECT 1$$x$$` errors *at the parser* with the token
`$$adjacent$$` — proving the lexer reads it as integer-then-string; `$t$ x $T$ y $t$` is one
string (exact, case-sensitive close); `$1 + 1` prepares as a positional parameter; and code after
a bare `\r` on a `--` line **executes** (returned 42).

### D1 — dollar-quote tag grammar was ASCII-only (re-review NEW-B1)

- **Defect:** `_DOLLAR_TAG_RE` matched `[A-Za-z_][A-Za-z0-9_]*` tags, but Postgres's grammar
  (scan.l `dolq_start [A-Za-z\200-\377_]`, `dolq_cont` adds `[0-9]`) admits ANY non-ASCII
  character — letter or not. A `$café$` body was scanned as code; a directive inside it was
  honored; `DROP TABLE` applied with exit 0 and no `--allow-destructive`.
- **The suggested fix was itself insufficient.** The re-review proposed `\$(?:[^\W\d]\w*)?\$`
  (Unicode *letters*). Verified against live PG16 that `$€$` (currency symbol, not a letter) and
  `$٣$` (Arabic-Indic digit — excluded by `[^\W\d]`) are BOTH valid tags: scan.l's rule is
  byte-based, not letter-based. Adopting the suggestion would have left the same gate open by
  the same mechanism.
- **Fix (`db/migrate.py:159-172`):** tag class translated faithfully from scan.l —
  `[A-Za-z_\u0080-\U0010ffff]` start, `[A-Za-z0-9_\u0080-\U0010ffff]*` continuation
  (`\200-\377` bytes ≡ code points ≥ U+0080 over UTF-8 text). Empty `$$` still matches; an ASCII
  digit still cannot start a tag (`$1` stays a positional parameter); the closing tag is still
  located by exact string search (`sql.find(tag)`), preserving exact/case-sensitive matching.
- **Two same-mechanism gaps closed alongside** (both were reachable classification errors):
  - `a$b$c` is ONE identifier in Postgres (`$` is `ident_cont`), but the scanner read its inner
    `$b$` as a quote opener and swallowed real code between two such identifiers (the re-review's
    reproduced `COMMIT`-swallowing case). Fixed with an identifier-continuation guard on `$`,
    bounded by the current code segment; a digit-only run does NOT suppress the opener because
    `1$$x$$` lexes as integer-then-string (verified live).
  - The E-string lookbehind used an ASCII identifier class, so `éE'…'` was lexed as an E-string
    when Postgres lexes identifier `éE` + a PLAIN literal — misplacing the literal's end and
    hiding a `COMMIT` from the tx lint. Lookbehind now uses the Postgres identifier alphabet.
- **Regression tests:** `db/tests/test_sql_scan.py` —
  `test_newb1_directive_inside_nonascii_tagged_body_is_not_honored` (parametrized over `café`,
  `é`, `€`, `٣`, `t1`), `test_newb1_unicode_tagged_do_body_is_not_a_tx_false_positive`,
  `test_newb1_dollar_inside_identifier_does_not_open_a_quote`,
  `test_newb1_dollar_quote_directly_after_integer_is_still_a_quote`,
  `test_newb1_estring_lookbehind_uses_postgres_identifier_alphabet`, plus round-trip and
  unterminated-token pins for the new tag forms. **End-to-end:**
  `db/tests/test_runner_db.py::test_newb1_regression_nonascii_dollar_tag_forgery_refused` — a
  migration with `DROP TABLE` plus a forged directive inside a `$café$` body is REFUSED (exit 1,
  table survives) without `--allow-destructive`, and applies cleanly WITH the flag (proving the
  tag really is a string body to a real PG16, not a false positive).

### D2 — own-line directive rule enforced only after code (re-review NEW-S1)

- **Defect:** `_directive_scan_text` judged "own line" on the REBUILT text, where every blanked
  segment becomes whitespace — so a directive trailing a multi-line `$$` body's closing line, a
  `*/`, or a multi-line literal reduced to leading whitespace and matched the `^`-anchored
  `DIRECTIVE_RE`. Both re-review shapes reproduced end-to-end (exit 0, table gone).
- **Fix (`db/migrate.py` `_own_line` + `_directive_scan_text`):** a `--` line comment survives
  the directive scan ONLY when everything before it on its line **in the raw source** is
  whitespace. Start-of-file, indented, after-a-comment-line, and CRLF directives remain honored;
  a directive trailing anything — code, `$$`, `*/`, a quote — is never honored.
- **Regression tests:** `test_news1_directive_trailing_a_multiline_dollar_body_is_not_honored`,
  `…_block_comment_…`, `…_literal_or_identifier_…`, and
  `test_standalone_directives_are_still_honored_everywhere_legitimate` (the no-false-negative
  side, incl. CRLF). **End-to-end:**
  `test_news1_regression_trailing_directive_after_dollar_body_refused`.

### D3 (found during this pass) — line comments ended only at `\n`

- **Defect:** Postgres ends a `--` comment at `\r` as well as `\n` (scan.l
  `non_newline [^\n\r]`); the scanner ran comments through to `\n`, so
  `-- note\rDROP TABLE t;` was classified all-comment while the server EXECUTES the `DROP`
  (verified live: code after a bare `\r` ran and returned a value). Same fail-open family as B-1.
- **Fix:** line comments terminate at the first of `\n`/`\r`; the terminator stays in code.
  `_own_line` treats `\r` as a line boundary. A directive on a CR-only line is deliberately NOT
  honored (`DIRECTIVE_RE` lines are `\n`-based) — fail-safe: classification falls to the sniff,
  which now correctly sees the code after the `\r`.
- **Regression tests:** `test_bare_cr_ends_a_line_comment_like_postgres`,
  `test_cr_only_line_endings_fail_safe`, plus CR/CRLF round-trip pins.

### Revert evidence (each defence broken independently on a scratch copy of `db/`)

| Revert | Test(s) that went RED |
|---|---|
| A1: `_DOLLAR_TAG_RE` → ASCII-only | 7 red: all 4 non-ASCII forgery params + unicode-DO false positive + round-trip + unterminated. **E2E:** `test_newb1_regression_…_refused` red with `assert 0 == 1` — forged DROP applied, table gone (the original bypass, reproduced verbatim) |
| A2: identifier-`$` guard removed | 2 red: `test_newb1_dollar_inside_identifier_…` + `a$b$c` round-trip |
| A3: E-string lookbehind → ASCII | 1 red: `test_newb1_estring_lookbehind_…` |
| B: own-line rule → rebuilt text | 3 red: all three `test_news1_…` unit tests. **E2E:** `test_news1_regression_…_refused` red with `assert 0 == 1` — trailing directive honored, DROP applied |
| C: comments end at `\n` only | 2 red: both CR tests |

Scratch copy lived in the session scratchpad and the fixed file was restored after each revert;
the repo tree was never used for revert experiments.

### Gates

```
$ .venv/bin/ruff check backend/app src db
All checks passed!
$ .venv/bin/python -m pytest -q
179 passed in 7.63s        (was 151; +28 from this pass)
$ shellcheck -x bin/*.sh
(clean, rc=0)
```

Live state: `rh-db` running and **healthy**; `bin/db_migrate.sh status` → 001/002/003 `applied`
/ checksum `ok`. The live DB was used read-only (SELECT-only lexer probes via `bin/db_psql.sh`);
no `km-*` object touched; `data/market/` untouched. The comment block above `DIRECTIVE_RE` and
the `_directive_scan_text` docstring were rewritten to state only the now-true guarantee — the
previous wording ("the scanner guarantees this") was the falsified claim and now holds for every
input in the corpus.

---

## Redesign — filename-marked destructiveness (round 4)

Implementer: independent — did not write or review rounds 1–3. Spec: the three failed
verifications (`REVIEW_migrate_runner.md`, `REVIEW_FIXES.md`, `REVIEW_FIXES_b1_residual.md`) and
the approved redesign brief. Recorded as **ADR-002**.

### Design

The SQL-text parser is gone. Classification and transaction safety now rest on signals no file
content can influence (the filename) or that come from the server itself (status + xid), with a
content sniff as a best-effort secondary net:

1. **Destructiveness = filename.** Grammar: `NNN_name.up.sql` / `NNN_name.down.sql`, destructive
   marked `NNN_name.destructive.up.sql` / `NNN_name.destructive.down.sql` (per direction).
   `FILENAME_RE` is the single source of truth; the name charset excludes `.`, so the marker can
   be neither smuggled nor faked, and near-miss spellings (`.destructiv`, `.Destructive`, wrong
   position, doubled) fail discovery loudly. Migrations 001–003: down files renamed to
   `*.destructive.down.sql` (`git mv`); up files untouched. **The renames orphan nothing:** the
   marker sits outside the `name` capture group, so recorded `version`/`name` are unchanged —
   verified live, `status` shows 001–003 `applied` / checksum `ok`, no ORPHAN rows. The inert
   `-- migrate: non-destructive` comments in the applied up bodies stay: the up body is
   checksummed, and editing an applied migration is forbidden by our own invariant. The directive
   lines were removed from the down files (down bodies are not checksummed).
2. **Keyword sniff at discovery.** `DROP TABLE|DROP SCHEMA|DROP DATABASE|TRUNCATE` searched
   on the RAW text (no comment/literal stripping — that would be the lexer again); a hit in an
   unmarked file refuses the run with the exact rename in the message.
   > **Correction (round 5).** This section originally claimed the sniff "cannot produce a false
   > negative from forged content". Round 4 refuted that (R4-B1): comment-separated keywords
   > (`drop/**/table` — a comment is a token separator to PostgreSQL) and dynamically built SQL
   > (`EXECUTE 'DR'||'OP TABLE …'`) both evaded it and applied unmarked with exit 0. The sniff is
   > a best-effort secondary net; the comment shapes are closed in Round 5 below, the dynamic-SQL
   > shapes cannot be closed by any text rule, and the author marking the filename is the real
   > control. Only the CLASSIFICATION (the filename) cannot be influenced by contents.
3. **Transaction ownership enforced by the server.** After the body, before the bookkeeping row:
   `conn.info.transaction_status` must be INTRANS **and** `pg_catalog.pg_current_xact_id()` must
   equal the xid captured at transaction start (schema-qualified so a body's `search_path` change
   cannot shadow it). On failure: `TxControlInMigration`, exit 1, nothing recorded.
4. **Byte-level rejection at discovery:** NUL (round-3 NEW2-S2), UTF-8 BOM (NEW2-N1), invalid
   UTF-8 (NEW2-N3) — one `read_bytes()` per file, clean one-line errors.

### Deleted

`_scan_sql`, `_blank`, `_rebuild`, `strip_sql_comments`, `strip_sql_noise`, `_own_line`,
`_directive_scan_text`, `contains_top_level_tx_control`, `explicit_destructiveness`,
`is_destructive`, `DIRECTIVE_RE`, `TX_CONTROL_RE`, `_IDENT_START_RE`, `_IDENT_CONT_RE`,
`_DOLLAR_TAG_RE`, the segment-kind constants, `SqlLexError`, `ConflictingDestructiveMarkers`
(~230 lines), and `db/tests/test_sql_scan.py` (the scanner-probe suite). The O(n²)
identifier-backscan died with the scanner; no remaining path is worse than linear (regex search +
byte scans), pinned by `test_discovery_is_fast_on_large_and_adversarial_files` (3 MB including
the old worst-case shape: ~0.01 s observed, 5 s bound).

### Empirical evidence — transaction-status check (throwaway PG16 container, psycopg 3.3.4)

Probe script executed each shape through `autocommit=True` + `with conn.transaction():`, exactly
as the runner does:

| body shape | status after body | xid | outcome |
|---|---|---|---|
| clean multi-statement | INTRANS | unchanged | passes |
| `…; COMMIT; …` | **IDLE** | n/a | status check fires |
| `…; ROLLBACK; …` | **IDLE** | n/a | status check fires |
| `…; COMMIT; BEGIN; …` | INTRANS (forged) | **changed** (746→747) | **xid check fires** |
| `…; BEGIN; …` (bare) | INTRANS | unchanged | tolerated — server no-op warning, atomicity intact |
| `…; SAVEPOINT sp1; …` | INTRANS | unchanged | tolerated — stays inside the runner's tx |

Also observed and now documented instead of assumed: raising inside the transaction block while
the connection is IDLE propagates our exception cleanly (psycopg's exit rollback tolerates IDLE);
after `COMMIT; BEGIN;` the exit rollback undoes the hijacker's second transaction (post-hijack
statements gone, bookkeeping never written); statements a stray COMMIT made durable stay durable —
the error message says to inspect and clean up manually, and the integration tests pin those
exact observed states (`test_stray_commit_…` asserts the leaked tables EXIST and the migration is
NOT recorded). BEGIN/SAVEPOINT tolerance is a deliberate, verified behavior change from the old
text-scan rejection.

### Revert-test evidence (each defence broken independently on a scratch copy of `db/`)

| Revert | Test(s) that went RED |
|---|---|
| R1: filename marker ignored (`up_destructive=False` always) | 6 red — `test_destructive_marker_parsed_per_direction`, `test_marked_file_with_destructive_sql_is_discovered`, `test_marked_file_without_destructive_keywords_is_allowed`, **E2E** `test_destructive_gate_blocks_and_flag_allows`, `test_dry_run_evaluates_the_gate_at_plan_time`, `test_marked_destructive_migration_ignores_forged_directive_text` |
| R2: sniff disabled | 24 red — all 12 keyword params, all 3 comment/literal params, rename-message, inert-directive, exit-code mapping, **and all 6 end-to-end forgery-corpus params** (`test_forged_contents_cannot_apply_a_drop_without_the_flag[*]`: the forged DROP applied, table gone) |
| R3: tx-intact check no-op | 4 red — stray-COMMIT, stray-ROLLBACK, COMMIT;BEGIN, down-hijack |
| R3b: xid comparison alone removed (status check kept) | exactly 1 red — `test_commit_begin_forging_intrans_is_caught_by_the_xid_check` (proves the xid check is independently load-bearing, not redundant with status) |
| R4: NUL check removed | 2 red — `test_nul_byte_rejected[up]`, `[down]` |
| Perf: new perf test vs the OLD implementation | old `_scan_sql` on the test's input: 4.37 s @ 10 kB, 17.70 s @ 20 kB (4× per doubling), still running at 30 s on the 1.5 MB perf-test input → the 5 s-bound test is red by timeout against the old code |

Forgery corpus pinned end-to-end (`FORGERY_BODIES`): round-1 dollar-body directive, round-2
`$café$` tag and trailing-`$$` directive, round-3 split-directive-from-code and U+00A0 own-line
forgery, plus a plain standalone directive — every one refused at discovery, `users` intact,
nothing recorded.

### Gates

```
$ .venv/bin/ruff check backend/app src db   → All checks passed!
$ .venv/bin/python -m pytest -q             → 163 passed (db suite 92 → 76: scanner probes deleted, redesign tests added)
$ shellcheck -x bin/*.sh                    → clean, rc=0
$ bash bin/db_migrate.sh down --allow-destructive --target 000  → 003, 002, 001 rolled back
$ bash bin/db_migrate.sh up                                     → 001, 002, 003 applied
$ bash bin/db_migrate.sh status             → 001/002/003 applied, checksum ok, no orphans
$ docker inspect rh-db … Health             → healthy
```

Docs updated: new `docs/adr/ADR-002-destructiveness-from-filename.md` (ADR-001 untouched),
`TESTS.md` (suite 3b now 76 tests, new guard-check list), `docs/PATTERNS_FROM_9B.md` (the
directive-classification advice replaced with the filename design and a do-not-rebuild warning).
Environment: probes ran against a throwaway `postgres:16-alpine` (`rh-probe-pg4`, removed after);
revert experiments lived in the session scratchpad, never the repo tree; no `km-*` object,
`data/market/`, or the `rh_db_data` volume touched; `rh-db` left running healthy.

---

## Round 5 — sniff hardening and claim correction

Scope: the five items from `REVIEW_redesign_verification.md` (round 4). The verifier validated the
core mechanism — filename classification (20/20 attacks blocked) and the transaction-status check
(22/22 hijacks caught) — so neither was touched, nor was the byte-level rejection. What changed is
the secondary sniff layer, the silent `.SQL` skip, one regex anchor, and every document that
claimed more than the code delivered. Fixer independent of rounds 1–4 and of the round-4 verifier.

### 1. Comment-tolerant sniff (R4-B1 shapes 1–3) — FIXED

PostgreSQL's lexer treats a comment as a token separator, so `drop/**/table users;` is a valid
`DROP TABLE` that the old `DROP\s+TABLE` regex could not see; seven bodies applied unmarked with
exit 0 in round 4. Adopted the verifier's separator alternation (`_SEP`: whitespace, `-- …` line
comments, one level of `/* … */` block comments) in `db/migrate.py`. The verifier's no-new-false-
positives claim was **re-verified independently before adoption**: the new regex was run over all
six files in `db/migrations/` — both hit columns identical to the old regex (up files clean, down
files hit and marked) — and over 11 must-miss shapes (`DROP INDEX/TRIGGER/CONSTRAINT/VIEW/ROLE`,
prose like "dropped tables", string literals, dynamic SQL) with zero hits. Performance stays
linear: 2 MB of `"DROP "` bait 0.075 s, `DROP` + a 2 MB block comment + `TABLE` 0.148 s; the
linearity test now includes the bait shape. The sniff error message truncates the matched text at
60 chars, since a comment-separated hit can span an arbitrarily long comment.

Known evasions that REMAIN, now documented instead of denied: nested block-comment separators
(PostgreSQL comments nest; a regex cannot), and everything in item 3 below.

### 2. Keyword list extended (R4-S1) — FIXED

`DROP OWNED` and `DROP MATERIALIZED VIEW` added to the alternation. `DROP OWNED BY rh_app` is the
realistic accident: `001_core_schema` creates that role, and retiring it would previously have
cascaded through every object it owns with exit 0, unmarked. `DROP VIEW` / `DROP ROLE` stay
un-sniffed deliberately (no stored rows lost), stated in the regex comment.

### 3. False claims struck — the load-bearing item

The refuted sentence existed in four places; each now states what is actually true — the FILENAME
is the authoritative classification and cannot be influenced by contents; the sniff is a
best-effort secondary net over the common literal shapes, deliberately incomplete (never covered
`DELETE FROM` / `DROP COLUMN`; cannot cover dynamic SQL — `EXECUTE 'DR'||'OP TABLE …'` contains no
keyword any text rule can see, and deciding whether arbitrary SQL destroys data would require
executing it); therefore the author marking the file correctly is the real control and the sniff
only reduces the cost of forgetting:

| Location | Old claim | Now |
|---|---|---|
| `db/migrate.py` docstring ("can only ever over-fire; cannot be forged into silence") | REFUTED | best-effort wording, holes enumerated, R4-B1 cited |
| `ADR-002` §Decision 2 ("cannot be forged into silence") | REFUTED | renamed **Best-effort sniff**, holes enumerated; §Consequences now names the sniff (not the filename) as the layer refusing the pinned corpus |
| this file §Design 2 ("Cannot produce a false negative from forged content") | REFUTED | correction block added in place, pointing here |
| `db/tests/test_runner_db.py` docstring ("prove file CONTENTS cannot influence it") | REFUTED | states the corpus exercises the sniff on shapes it can see, and that dynamic SQL applies unmarked |

Also corrected: `docs/PATTERNS_FROM_9B.md` ("the gate cannot be forged from contents" →
classification cannot be; sniff best-effort), `TESTS.md` suite 3b ("proving file CONTENTS cannot
bypass the gate" → classification unforgeable + sniff refuses literal shapes), and every
"fail-closed sniff" label in code, tests, and docs. A final sweep for the overclaim phrases
(`forged into silence`, `cannot produce a false negative`, `CONTENTS cannot influence/bypass`,
`only ever over-fire`, `fail-closed sniff`) finds them only in the historical `REVIEW_*` reports,
which record what was claimed at the time and (in round 4's case) refute it.

The documented reality is pinned as executable tests (R4-S3): three dynamic-SQL shapes and a
`DELETE FROM` body discover cleanly as non-destructive (`test_dynamic_sql_is_invisible_to_the_
sniff_as_documented`, `test_delete_from_is_not_sniffed_marker_is_the_control`), and one runs
end-to-end against a real Postgres — exit 0, no flag, `users` gone, migration recorded
(`test_dynamic_sql_drop_applies_unmarked_documented_limitation`). Those tests fail the moment the
sniff starts catching more than the docs say, so docs and code cannot silently diverge again in
either direction. The forgery corpus gained the round-4 `drop/**/table` shape
(`comment-separated-drop`) and its header now names the sniff as the refusing layer.

### 4. `.SQL` silent skip (R4-S2) — FIXED, rejection chosen over adoption

Old behavior: `002_x.up.SQL` was dropped by `path.suffix != ".sql"` before `FILENAME_RE` ran, so
`up` printed "no pending migrations", exited 0, and `status` never mentioned the file — a
migration reported as success that never happened. Discovery now refuses loudly any non-directory
entry that plausibly wants to be a migration: a version-like prefix (`\d+_`), or a `.sql`-like
extension after case-folding and stripping trailing space/dot/newline junk. A directory or
dangling symlink whose NAME matches the grammar is refused too (same silent-skip mode via the old
`is_file()` guard). Unrelated entries (`README.md`, `.gitkeep`, subdirectories) are still ignored
— pinned by test.

**Why reject rather than accept `.SQL` case-insensitively:** the grammar is all-lowercase and
exact everywhere else (name charset, marker, direction — round 4 proved near-miss *spellings* fail
loudly), so adopting a case-insensitive extension would fork the grammar and create
`002_x.up.sql` + `002_x.up.SQL` duplicate ambiguity that would need its own conflict detection.
A loud one-line rename error at discovery costs the author seconds; silence was the only
unacceptable outcome, and loud-reject preserves "one spelling, exactly" as a uniform property.

### 5. `FILENAME_RE` anchored with `\Z` (NIT-1) — FIXED

`$` also matches before a trailing newline in Python, so the grammar accepted `"002_x.up.sql\n"`
— previously masked by the R4-S2 suffix skip, and unmasked by fixing it (with `$` and the new
loop, a trailing-newline file would have been processed as a real migration). `\Z` matches only at
the true end. Pinned directly against the regex so no other layer can mask it again.

### Regression-test evidence — reverts on a scratch copy (never the repo tree)

Scratch copy of `db/` + `pyproject.toml` in the session scratchpad; baseline there: **104 passed**
(76 + 28 new). One revert at a time, each restored before the next:

| Revert | Red | Names |
|---|---|---|
| Item 1: `_SEP` → `\s+` (keyword list kept) | **exactly 11** | all 10 `test_comment_separated_keywords_refused[{up,down}×5]` + `test_forged_contents_cannot_apply_a_drop_without_the_flag[comment-separated-drop]`. The item-3 pin tests stayed GREEN under this revert — they assert a limitation that predates and survives the fix, not the fix itself |
| Item 2: drop `OWNED\|MATERIALIZED` | **exactly 2** | both `test_drop_owned_and_drop_materialized_view_refused` params |
| Item 3: sniff drifted to catch `DELETE`/`EXECUTE` (simulating docs under-claiming) | 9, incl. **all 5 pin tests** | 3 × `test_dynamic_sql_is_invisible_to_the_sniff_as_documented`, `test_delete_from_is_not_sniffed_marker_is_the_control`, `test_dynamic_sql_drop_applies_unmarked_documented_limitation` (+4 legit-migration tests broken by the overbroad regex) — the pins are live assertions, not tautologies |
| Item 4: restore silent `.suffix != ".sql"` skip | **exactly 8** | all 6 `test_near_miss_migration_files_are_refused_not_skipped` params + `test_uppercase_sql_extension_fails_the_run_loudly` + `test_directory_named_like_a_migration_is_refused` |
| Item 5: `\Z` → `$` | **exactly 2** | `test_filename_re_rejects_trailing_newline` + `test_near_miss…[002_extra.up.sql\n]` (with `$` the grammar wrongly matches the name, so the file is processed and dies as `MissingPair` instead of the loud near-miss refusal) |

### Gates

```
$ .venv/bin/ruff check backend/app src db   → All checks passed!
$ .venv/bin/python -m pytest -q             → 191 passed (was 163; db suite 76 → 104)
$ shellcheck -x bin/*.sh                    → clean, rc=0
$ bash bin/db_migrate.sh down --allow-destructive --target 000  → 003, 002, 001 rolled back
$ bash bin/db_migrate.sh up                                     → 001, 002, 003 applied
$ bash bin/db_migrate.sh status             → 001/002/003 applied, checksum ok, no orphans
$ docker inspect rh-db … Health             → healthy
```

Untouched by instruction: the filename grammar (beyond the `\Z` anchor the verifier specified),
the transaction-status + xid check, the byte-level encoding rejection. Environment: integration
tests via testcontainers (throwaway `postgres:16-alpine`, dies with the session); revert
experiments in the session scratchpad only; no attack file ever entered `db/migrations/`; no
`km-*` container/volume/network, `data/market/`, or the `rh_db_data` volume touched; `rh-db` left
healthy with 001–003 applied.
