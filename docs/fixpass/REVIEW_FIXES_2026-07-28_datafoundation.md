# Review: fix-pass for the 3b data foundation

**Re-reviewer:** independent (did not write the code, did not write the three original reviews, did
not perform the fix-pass) · **Date:** 2026-07-28 · **Tree:** working tree on top of `f0e49ee`

*(The previous contents of this file — the June dashboard re-review — are preserved in git history
at `bb5be0e`, mirroring what the fix-pass did with `FIX_REPORT.md`.)*

**Scope:** every BLOCKER / SHOULD-FIX / NIT / PRAISE item in `REVIEW_migrate_runner.md` (2 B, 6 SF,
7 N), `REVIEW_schema.md` (2 B, 8 SF, 4 N) and `REVIEW_db_infra.md` (5 B, 11 SF, 9 N), plus the
`FIX_REPORT.md` claims, plus a search for defects the fix-pass itself introduced.

**Method.** Every finding below was executed, not read. Regression tests were validated by
*deliberately re-breaking each fix on a scratch copy of `db/`* and confirming the test turns red.
The live `rh-db` was used read-only except for the one round trip the brief required
(`down --target 000` → `up`), which was preceded by a verified `pg_dump`. Two throwaway containers
(`rhrev2-porttest`, `rhrev-initdb-test`) and one throwaway volume were created and removed. No
`km-*` object was modified or stopped; all 11 `km-*` containers were healthy before and after.
`git status` at the end is byte-identical to the state I inherited — none of my experiments touched
the repo.

---

## Summary verdict

**PASS WITH CONDITIONS.**

This is a strong fix-pass. Eight of the nine BLOCKERs are genuinely, verifiably closed, each with a
regression test that I confirmed *fails* when I revert the specific defence it guards. The schema
work is better than what it replaced in every dimension the reviewers named, the container
hardening claims are all true on the live container, and — the thing that most distinguishes it —
the comments that were the subject of five of the blockers now say only what I could reproduce.
The ADR-001 correction is unusually honest.

The condition is that **B-1 is not fully fixed**. The regex-triple was correctly replaced by a
single-pass scanner, and the four documented fail-open cases are dead. But the scanner's
dollar-quote tag grammar (`db/migrate.py:156`) is ASCII-only while PostgreSQL's is not, so a
directive inside a `$café$ … $café$` body is still honored — and I reproduced the original blocker
end-to-end against the *fixed* runner: `DROP TABLE users;` applied with exit 0 and no
`--allow-destructive`, table gone. The module docstring at `db/migrate.py:94-96` states the
opposite as a guarantee. Separately, the documented "directive must sit on its own line" rule is
enforced only when the preceding text on that line is code, which produces two more reproduced
gate bypasses. Both live in the same function and are ~5 lines apart.

Ship after those two are closed. Everything else is follow-up tickets.

---

## Finding-by-finding verification

Status key: FIXED · PARTIALLY-FIXED · NOT-FIXED · REGRESSION-INTRODUCED · DEFERRED-WITH-DOC ·
REJECTED-WITH-RATIONALE.

### REVIEW_migrate_runner.md

| Finding ID | Source review | Original severity | Fix status | Notes |
|---|---|---|---|---|
| B-1 | migrate_runner | BLOCKER | **PARTIALLY-FIXED** | Scanner replaces the three regex passes; all four documented bypasses dead and regression-tested (I broke each and watched the tests fail). **Residual: 3 newly reproduced bypasses of the same gate** — see NEW-B1 / NEW-S1. `db/migrate.py:156, 293-302` |
| B-2 | migrate_runner | BLOCKER | FIXED | `db/tests/` exists: 64 tests (24 scanner, 22 discovery/CLI, 9 testcontainers). I confirmed testcontainers really starts a `postgres:16-alpine` (watched it in `docker ps`), and that the suite is wired into `pyproject.toml` `testpaths` and `TESTS.md:43-53`. |
| SF-1 | migrate_runner | SHOULD-FIX | FIXED | `validate_target` (`db/migrate.py:418-436`) runs before `connect_from_env`; `main(["up","--target","2"])` returns 1 with no DB configured, proving the ordering. |
| SF-2 | migrate_runner | SHOULD-FIX | FIXED | `pg_advisory_lock` at `db/migrate.py:482`; serialization confirmed live (a runner blocked 22 s behind a held lock). **The wait is unbounded and silent — NEW-S2.** |
| SF-3 | migrate_runner | SHOULD-FIX | FIXED | Verified live against a blackholed host: default 10 s, `MIGRATE_CONNECT_TIMEOUT=3` → 3 s, `=abc` → clean validation error. `try/except psycopg.Error: conn.close()` present at `:483-485`. |
| SF-4 | migrate_runner | SHOULD-FIX | FIXED | Bodies cached on the frozen dataclass (`:343-346`); `test_discovers_in_order_with_cached_text_and_checksum` rewrites the file post-discovery and asserts the cached text is used. |
| SF-5 | migrate_runner | SHOULD-FIX | FIXED | `db/migrate.py:377-382`; test uses the exact `999`/`1000` pair from the review. |
| SF-6 | migrate_runner | SHOULD-FIX | FIXED | `_Parser.error` → exit 1 (`:657-667`); `main(["frobnicate"])==1`, `main(["--help"])==0`, both tested. |
| N-1 | migrate_runner | NIT | FIXED | `finally: conn.close()` gone; `:713-714` explains ownership and lock release. |
| N-2 | migrate_runner | NIT | FIXED | `:682` help text now states the bookkeeping-DDL behaviour. |
| N-3 | migrate_runner | NIT | FIXED | Nested block comments and `"begin"` identifiers are in the no-false-positive table; both go red when I restore the old strippers. |
| N-4 | migrate_runner | NIT | FIXED | `/* migrate: … */` proven inert (`test_directive_inside_block_comment_is_not_honored`). The *own-line* half of the same comment is only partly true — NEW-S1. |
| N-5 | migrate_runner | NIT | **DEFERRED-WITH-DOC** | Sound. `down_checksum` needs a column on an existing `schema_migrations`, which `CREATE TABLE IF NOT EXISTS` cannot add; that is a deliberate internal-migration step, not fix-pass scope. No correctness impact. |
| N-6 | migrate_runner | NIT | FIXED | `_warn_orphans` (`:532-540`) called from both `cmd_up` and `cmd_down`; I invoked it directly and got the warning. |
| N-7 | migrate_runner | NIT | FIXED | `LIMITATION` section at `db/migrate.py:37-40`. |
| P-1…P-5 | migrate_runner | PRAISE | PRESERVED | `autocommit=True` + `conn.transaction()` intact (`:471, :503`) and now covered by `test_failed_migration_rolls_back_body_and_bookkeeping_together`, which I re-ran green. Checksum-before-plan, discovery-time validation, plan-time gate incl. `--dry-run`, `clock_timestamp()`, parameterized bookkeeping — all present. |

### REVIEW_schema.md

| Finding ID | Source review | Original severity | Fix status | Notes |
|---|---|---|---|---|
| B1 | schema | BLOCKER | FIXED | Live catalog: `uq_fundamentals_snapshot … (security_id, period_end, period_type, source_id, known_at) NULLS NOT DISTINCT`. Restatement coexistence, identical-observation dedupe, and NULL-source/NULL-`known_at` collision all asserted in `test_real_migrations_up_down_up`. **Break test:** restoring the `COALESCE(source_id,0)` key makes that test fail with the exact `UniqueViolation` the review reported. `ON DELETE RESTRICT` on `fk_fundamentals_source` verified (`confdeltype='r'`). |
| B2 | schema | BLOCKER | FIXED | Live: 62 minute partitions, **0 DEFAULT**. I independently parsed the real `2020-11-30.csv.gz`: `max(window_start)` = `2020-12-01 00:59 UTC` — the spillover is real, and the test asserts the bar lands in `price_bars_minute_2020_12`. **Break test:** re-adding a DEFAULT partition and removing the pre-create block makes the assertion fail with `price_bars_minute_default`. Out-of-range insert raises `no partition of relation`. |
| F1 | schema | SHOULD-FIX | FIXED | `fundamentals_asof()` (`003:143-160`) pins `known_at IS NOT NULL AND known_at <= p_asof`; the test proves pre/post-restatement values and NULL-`known_at` invisibility. |
| F2 | schema | SHOULD-FIX | FIXED | **Independently reproduced against the real corpus.** Three day files (2020-11-30, 2021-06-30, 2023-06-30): new regex rejects **0** of 13,667 distinct tickers; old regex rejects 482 / 466 / 411 respectively — matching the "482 in the first file" claim exactly. (Count discrepancy → NEW-N5.) |
| F3 | schema | SHOULD-FIX | FIXED | `uq_securities_symbol_live … WHERE delisted_at IS NULL` + `ix_securities_symbol`; recycled-ticker behaviour asserted in the integration test. |
| F4 | schema | SHOULD-FIX | FIXED | The reviewer's own recommendation taken, not four blind indexes: all four `source_id` FKs RESTRICT (verified `confdeltype='r'` on all 7 named FKs), append-only policy at `001:63-67`, index only on `fundamentals_snapshots` (`003:126`), deviation documented at `002:39-41`. `DELETE FROM data_sources` refusal asserted. |
| F5 | schema | SHOULD-FIX | FIXED | `is_active` dropped entirely (the stronger option); no consumer anywhere in the repo (grepped). |
| F6 | schema | SHOULD-FIX | FIXED | Live catalog: 7 distinct `fk_…` names, zero `_fkey` auto-names. |
| F7 | schema | SHOULD-FIX | FIXED | `data_sources` and `price_bars_daily` both have `updated_at` + trigger; the minute-table omission is documented with its ~26 GB cost at `002:56-59`. |
| F8 | schema | SHOULD-FIX | FIXED | `ix_fundamentals_screen` gone; the NOTE at `003:128-131` records the `EXPLAIN (ANALYZE, BUFFERS)` bar a successor must clear. |
| N1 | schema | NIT | FIXED | `known_at >= (period_end::timestamp AT TIME ZONE 'UTC')` at `003:99`; violation asserted in the test. |
| N2 | schema | NIT | FIXED | Both `to_regclass` and `CREATE TABLE` schema-qualified (`002:100-104`). |
| N3 | schema | NIT | FIXED | Redundant `high >= low` CHECKs removed on both bar tables, with the implication recorded at `002:47-48` and `002:154`. |
| N4 | schema | NIT | FIXED | `first_seen` convention in-column and via `COMMENT ON COLUMN` (`001:130-132, 172-174`). |
| P1…P5 | schema | PRAISE | PRESERVED | `period_end`/`known_at` split strengthened, not simplified; `unparsed` JSONB intact (`003:73-78`); `data_sources` provenance / `fetched_at` / sha dedup intact; delisted retention strengthened by F3/F5; down files still carry `migrate: destructive` with explicit data-loss statements. |

### REVIEW_db_infra.md

| Finding ID | Source review | Original severity | Fix status | Notes |
|---|---|---|---|---|
| B1 | db_infra | BLOCKER | FIXED | **Empirically proved.** I ran the exact `python3 - "$src" "$dst" <<'PY'` block from `bin/db_up.sh:49-58` while snapshotting every `/proc/*/cmdline` on the box (1,935 argv samples over 4 s): the generated 43-char password appears in **none** of them. The awk pipeline is gone. |
| B2 | db_infra | BLOCKER | FIXED | Same run: `stat` immediately after creation reports **600**. `O_EXCL` refuses an existing file (`FileExistsError`). For contrast, a plain redirect under this box's umask 0002 still yields 664 — the original defect, now structurally impossible. |
| B3 | db_infra | BLOCKER | FIXED **(the live 9b hazard)** | Live: 1840 / 1841 / 1842 / 1843 all report **BUSY**. **Break test:** I restored the old `docker ps --format '{{.Ports}}' \| grep -oE ':[0-9]+->'` implementation, created a *stopped* throwaway container publishing `127.0.0.1:30222-30223`, and reproduced the defect — old parser says FREE for both, new parser says BUSY for both. `docker ps -a` renders `PORTS=[]` for it, confirming the review's Defect 2; `HostConfig.PortBindings` survives the stop. Range-valued `HostPort` expansion verified independently through the awk. |
| B4 | db_infra | BLOCKER | FIXED | `db/.env.example:17-24` gives the real two-step procedure (`\password rh` first, then the file) and states outright that `POSTGRES_PASSWORD` is initdb-only and recreation rotates nothing. |
| B5 | db_infra | BLOCKER | FIXED | Compose header (`docker-compose.db.yml:13-25`) now states the verified truth including "not stronger" than a loopback bind; `DB_PORT` / `.env.db` / `pick_db_port.sh` references gone from all three files (grepped). ADR-001 carries a dated amendment with a "What this does NOT provide" section and the corrected Bad bullet. Egress block re-verified live: `nc 1.1.1.1 443` rc=1, external DNS rc=2. |
| S1 | db_infra | SHOULD-FIX | FIXED | No URL is assembled anywhere; `db_migrate.sh:70-77` exports `PG*` and passes them by name, `connect_from_env` accepts the empty DSN (`db/migrate.py:451-463`). Proved end-to-end — `status`, `down --target 000` and `up` all ran through this path against the live DB. |
| S2 | db_infra | SHOULD-FIX | FIXED | `grep -rn "source .*\.env"` over `bin/` + `Makefile` shows no `db/.env` sourcing; compose uses `--env-file`, `db_migrate.sh` uses `read_env_value` (`:34-38`). The `${VAR:?}` guards are preserved in compose. |
| S3 | db_infra | SHOULD-FIX | FIXED | Live: `statement_timeout=1min`, `idle_in_transaction_session_timeout=5min`. The comment (`docker-compose.db.yml:58-63`) now describes what the code does, and `migrate.py:475-480` documents its session-scoped opt-out. |
| S4 | db_infra | SHOULD-FIX | FIXED | Live probe comparison inside the container: real → 0, bogus db → **2**, bogus user+db → **2**, while `pg_isready -U nosuchuser -d nosuchdb` still returns 0. Both properties the review said to preserve (127.0.0.1 rationale, initdb-window fail-closed) are documented at `:98-108`. |
| S5 | db_infra | SHOULD-FIX | FIXED | `.github/workflows/image-scan.yml`: Trivy on both images, HIGH/CRITICAL + `ignore-unfixed`, weekly cron, CycloneDX SBOM, all three actions SHA-pinned. The `gosu` scope decision is sound — see below; I confirmed `postgres:16-alpine` really does ship `/usr/local/bin/gosu` (1.19, go1.24.6), so the rationale is about a real artifact, not a guess. |
| S6 | db_infra | SHOULD-FIX | FIXED | **Hashes independently verified against PyPI**: all three sha256 values in `db/requirements.txt` match real artifacts — `psycopg-3.3.4-py3-none-any.whl`, `psycopg_binary-3.3.4-cp312-…manylinux_2_17_x86_64.whl`, `typing_extensions-4.16.0-py3-none-any.whl`. Platform caveat → NEW-N3. |
| S7 | db_infra | SHOULD-FIX | FIXED | `db/.dockerignore` present with `.env`, `.env.*`, `!.env.example`, `__pycache__/`, `*.pyc`, `tests/`, and the context-relative gotcha explained in-file. |
| S8 | db_infra | SHOULD-FIX | FIXED | Live container: `ReadonlyRootfs=true`, `Memory=MemorySwap=2147483648`, `PidsLimit=512`, `NanoCpus=2000000000`, `touch /etc/probe` → read-only FS. I also proved the untested half: a **fresh volume** initdb under the same read-only + tmpfs + cap set completes and the server comes up. |
| S9 | db_infra | SHOULD-FIX | FIXED | Live: `rh_app` exists, `rolsuper=f`, no password in `pg_authid`. It can SELECT `securities`, and is refused on `schema_migrations`, `CREATE TABLE`, and `COPY … TO PROGRAM`. `pg_default_acl` shows `defaclrole=rh` — correctly scoped, not global; `schema_migrations.relacl` is NULL, i.e. deliberately ungranted, exactly as claimed. One untrue sentence in the comment → NEW-S3. |
| S10 | db_infra | SHOULD-FIX | FIXED | Ran `bin/db_backup.sh` live: dump written to `.partial`, `pg_restore --list` verified, renamed, retention applied. Volume is `external: true` in compose and created by `db_up.sh:77`. |
| S11 | db_infra | SHOULD-FIX | FIXED | `rh.build_inputs_sha256` label on `rh-migrate:local` equals `sha256sum` of `db/Dockerfile` + `db/requirements.txt` today — the comparison at `db_migrate.sh:55-62` is live and correct. |
| N1 | db_infra | NIT | FIXED | Bounds travel as `sys.argv` (`lib_ports.sh:107-113`); `PORT_MAX < PORT_MIN` yields one line. (A non-integer bound still tracebacks → NEW-N6.) |
| N2 | db_infra | NIT | FIXED | `local exclude="$*"` (`:101`). |
| N3 | db_infra | NIT | FIXED | Header `:7-10` drops "authoritative" and states the IPv6/UDP blind spots and the AND-of-three reasoning. |
| N4 | db_infra | NIT | FIXED | `--user postgres` at `db_psql.sh:31`; the PGPASSWORD comment is truthful about in-container trust. |
| N5 | db_infra | NIT | FIXED | `RH_DB_CONTAINER` honoured by both `db_psql.sh:17` and `db_backup.sh:23`. |
| N6 | db_infra | NIT | FIXED | Live `StopTimeout=60`. |
| N7 | db_infra | NIT | FIXED | Credential reads at `db_migrate.sh:47-49` precede the build at `:55-62`. |
| N8 | db_infra | NIT | **REJECTED-WITH-RATIONALE** | Sound. The original review itself conceded the shebang buys editor/shellcheck detection and that the execution guard makes it harmless. |
| N9 | db_infra | NIT | FIXED (structurally) | `__pycache__/` is in `.gitignore:2` and in `db/.dockerignore`. Note `db/__pycache__/` regenerates every time the new suite runs — the durable fix is the ignore rules, not the deletion. |
| P1…P10 | db_infra | PRAISE | PRESERVED | See "Were any PRAISE items silently undone?" below — all ten re-verified individually. |

**Totals: 51 FIXED · 1 PARTIALLY-FIXED · 0 NOT-FIXED · 0 REGRESSION-INTRODUCED · 1 DEFERRED-WITH-DOC
· 1 REJECTED-WITH-RATIONALE.**

---

## Regression-test verification

For each BLOCKER: I reverted the specific defence on a scratch copy of `db/` and re-ran the suite.
A test that does not go red here is not a regression test.

| Blocker | Defence I reverted | Test(s) that went RED | Verdict |
|---|---|---|---|
| runner B-1 (case 1: dollar-body directive) | `_directive_scan_text` keep-set `{_CODE,_LINE_COMMENT}` → `{_CODE,_LINE_COMMENT,_DOLLAR}` | `test_b1_directive_inside_dollar_quoted_body_is_not_honored`; `test_b1_regression_forged_directive_does_not_bypass_gate` (end-to-end: `assert 0 == 1`) | **catches** |
| runner B-1 (cases 2-4) | `strip_sql_comments` / `strip_sql_noise` replaced by the old regex passes (dollar-strip-first) | `test_b1_line_comment_marker_inside_literal_does_not_swallow_sql`, `test_b1_literal_containing_comment_markers_does_not_hide_tx_control`, `test_b1_dollar_signs_inside_two_comments_do_not_pair_up`, plus 3 no-false-positive / E-string cases and `test_strip_views_are_consistent` — 7 red | **catches** |
| runner B-2 (no tests) | n/a — the tests *are* the fix | 64 collected; 9 run against a real `postgres:16-alpine` container which I watched start in `docker ps` | **satisfied** |
| schema B1 (restatements) | `(…, source_id, known_at) NULLS NOT DISTINCT` → `(…, COALESCE(source_id,0))` | `test_real_migrations_up_down_up` — `UniqueViolation … Key (…, COALESCE(source_id, 0::bigint))=(3, 2021-03-31, quarterly, 0) already exists` | **catches** |
| schema B2 (partition wedge) | dropped the 62-partition pre-create, restored `price_bars_minute_default` | `test_real_migrations_up_down_up` — `assert 'price_bars_minute_default' == 'price_bars_minute_2020_12'` | **catches** |
| infra B1 (password in argv) | n/a — proved directly by scanning 1,935 `/proc/*/cmdline` samples during a real run | password absent from every sample | **verified, no test** |
| infra B2 (0664 window) | n/a — proved directly (`stat` at creation = 600; `O_EXCL` refuses; plain redirect under umask 0002 = 664) | | **verified, no test** |
| infra B3 (ports) | restored the old `docker ps \| grep ':[0-9]+->'` parser; created a **stopped** container publishing a range | old parser: 30222/30223 FREE. new parser: both BUSY | **catches, no test** |
| infra B4 (rotation doc) | n/a — documentation | claim cross-checked against the image's initdb-only behaviour | **verified** |
| infra B5 (reachability claims) | n/a — documentation | claims re-verified against the live container (egress rc=1/rc=2, no host port, on-box bridge reachable) | **verified** |

Gap worth naming: **the three infra blockers with reproducible mechanics (B1, B2, B3) have no
automated regression test.** B3 in particular is a parser over a Docker output format, and the
original review explicitly asked for "a regression check for both the range form and the
stopped-container form — this is the kind of parsing that rots silently"
(`REVIEW_db_infra.md:373-374`). That request was not honoured. See NEW-S4.

---

## Bar checklist (post-fix state)

| Bar rule | Verdict | Evidence |
|---|---|---|
| §0 [P0] Robust by default | PASS | Explicit connect timeout (measured 10 s / 3 s), fail-closed `.env` paths, `.partial`-then-rename backups, idempotent `db_up.sh` (re-ran: no container recreation, same container ID and `StartedAt`). |
| §0 [P0] Fail closed, fail loud | **PARTIAL** | Both gates now fail closed on the documented inputs; `SqlLexError` rejects invalid SQL at discovery. **But the destructive gate still fails OPEN on three reproduced inputs** (NEW-B1, NEW-S1). |
| §0 [P0] Comments explain *why*, and are true | **PARTIAL** | The cross-cutting theme was executed well — I re-tested every claim the infra review falsified and all now hold. Three claims are still untrue: `migrate.py:94-96` (dollar-body directives "never honored"), `migrate.py:96` (own-line rule), `001:36-39` (`rh_app` "cannot authenticate"). |
| §0 [P0] Clean tree | PASS | No debug residue, no un-ticketed TODOs, no secrets. `db/__pycache__` regenerates from test runs but is ignored everywhere it matters. |
| §1.2 [P0] Full type hints, no `Any` | PASS | `db/migrate.py` fully annotated; ruff clean. |
| §1.8 [P0] Explicit timeout on every outbound I/O | **PARTIAL** | `connect_timeout` fixed. The advisory-lock acquisition is unbounded, and `SET statement_timeout = 0` is issued *before* it, removing the only thing that would have bounded it (NEW-S2). |
| §3.5 No unvalidated interpolation into an interpreter | PASS | `lib_ports.sh` bounds now travel as argv; `db_psql.sh` argv passing re-verified with metacharacters. |
| §3.6 [P0] No secrets in git / argv / images / loose modes | PASS | Verified: 0600 at creation, absent from argv, `db/.dockerignore` keeps it out of the build context, gitignored. |
| §3.11 / §6.10 Pin + hash + SCA | PASS | Both base digests are live multi-arch OCI indexes (checked upstream); all three wheel hashes match PyPI; Trivy + SBOM in CI with SHA-pinned actions. |
| §4.1 [P0] FK explicit `ON DELETE`, indexed or documented | PASS | 7 named FKs, all `RESTRICT`; the two unindexed ones carry a written, costed deviation. |
| §4.2 [P0] Name every constraint/index | PASS | Zero `_fkey` auto-names in the live catalog. |
| §4.3 [P0] Audit columns + trigger | PASS | Present on all mutable tables; the minute-table omission is documented with its cost. |
| §4.5 [P0] Both directions tested; destructive gated | **PARTIAL** | Full `up → down → up` round trip run live with **zero residue** (1 table = bookkeeping, 0 public functions, no `rh_app`, no default ACLs) and again in the test suite. Gate still forgeable (NEW-B1). |
| §4.8 Server-side timeouts | PASS | `statement_timeout=1min`, `idle_in_transaction_session_timeout=5min` live. |
| §4.9 [P0] Least-privilege runtime role | PASS | `rh_app` verified DML-only, non-superuser, denied `COPY … FROM PROGRAM` and `schema_migrations`. |
| §4.10 [P0] Automated, *tested* backups | PASS (scheduling deferred) | `pg_restore --list` verification is the right bar and it ran. Cron is explicitly the operator's; off-host is explicitly out of scope. Both stated, not hidden. |
| §5.2 [P0] Regression test per bug fix | PASS (with a gap) | Verified by breaking each of the five code-level blockers. The three infra blockers have no test (NEW-S4). |
| §6.1 / §6.3 Container hardening | PASS | `ReadonlyRootfs=true`, memswap = mem, pids 512, `no-new-privileges`, `cap_drop ALL` + 5 caps, `StopTimeout=60`, digest-pinned, numeric non-root `USER` — all confirmed on the live container, plus a fresh-initdb smoke test under the same constraints. |
| §6.7 Liveness vs readiness | PASS | Healthcheck now fails (exit 2) on a bogus role or database. |
| Standing rule: verify port free, never disturb the holder | PASS | 1840-1843 BUSY; stopped-container and range forms both caught. |
| Namespace isolation from 9b | PASS | No `down` / `-v` / `prune` / `rm` added to any DB script; volume `external: true`; all 11 `km-*` containers healthy throughout. |
| §7.2 [P0] Guardrails tunable, observable, never silently block | **PARTIAL** | The destructive gate is loud and overridable; `down --target` no longer silently no-ops. But the advisory lock blocks silently and indefinitely — including a read-only `status` (NEW-S2). |

---

## New findings introduced by the fix-pass

### BLOCKER (new)

- **NEW-B1** — The scanner's dollar-quote grammar is narrower than PostgreSQL's, so **B-1 case 1
  still reproduces** with a non-ASCII tag. `db/migrate.py:156`. Demonstrated end-to-end against the
  fixed runner: `DROP TABLE users;` applied, exit 0, no `--allow-destructive`, table gone.

### SHOULD-FIX (new)

- **NEW-S1** — The documented "a directive must sit on its own line" rule (`db/migrate.py:96`, and
  the `_directive_scan_text` docstring at `:293-302`) holds only when the preceding text on that
  line is *code*. After any blanked token — a multi-line dollar body, block comment, string
  literal, or quoted identifier — a trailing directive **is** honored. Two more end-to-end gate
  bypasses.
- **NEW-S2** — The advisory lock (`db/migrate.py:482`) blocks forever, silently, and the
  `SET statement_timeout = 0` two lines above it (`:479`) removes the server-side 60 s bound that
  would otherwise have capped the wait. A read-only `status` is blocked by a running `up`.
- **NEW-S3** — `db/migrations/001_core_schema.up.sql:36-39` claims `rh_app` "cannot authenticate
  until an operator sets one". False on the in-container loopback and unix socket, where the
  image's `pg_hba` is `trust`. Exposure is nil (that path already grants superuser `rh`), but this
  is the exact class of untrue security comment the infra review made five blockers out of, and
  `bin/db_psql.sh:34-37` states the opposite about the same `pg_hba`.
- **NEW-S4** — No regression test for infra B1 / B2 / B3, despite the original review explicitly
  asking for one on B3. All three are cheaply testable.

### NIT (new)

- **NEW-N1** — The pre-created partition window ends **2025-11**, but today is 2026-07: there is no
  partition for the current month (`to_regclass('public.price_bars_minute_2026_07')` is NULL).
  Fail-loud, and no loader exists yet, so nothing breaks today — but the first live (non-archive)
  bar will error until someone calls the helper. `002_price_bars.up.sql:118-125`
- **NEW-N2** — `_scan_sql` is O(n²) on a pathological nested-block-comment shape (2 MB of spaced
  `/*` then all closes → 991 ms; 4× length → 4× time). Irrelevant for repo-authored files; worth a
  note next to the "one left-to-right pass" claim, which reads as a linearity guarantee.
- **NEW-N3** — `db/requirements.txt:12-13` pins a **cp312 / manylinux-x86_64-only** wheel hash while
  `db/Dockerfile:14` pins a multi-arch index digest. An arm64 build now fails at `pip install`.
  Fail-loud and documented in the requirements header; still an asymmetry worth naming in the
  Dockerfile.
- **NEW-N4** — `bin/db_backup.sh:33-34` chmods the *directory* to 700 but leaves each dump at the
  umask mode (observed 664). The directory protects it today; `umask 077` or an explicit
  `chmod 600` on the file would make it structural, matching the posture `db/.env` now has.
- **NEW-N5** — `001_core_schema.up.sql:146-148` records "12,928 distinct tickers" across the three
  named day files. I measure **13,667** distinct across the union of exactly those three files. The
  load-bearing claim (0 rejected; 482 rejected by the old regex in the 2020-11-30 file) reproduces
  precisely; only the count does not.
- **NEW-N6** — `bin/lib_ports.sh:105-109` says the bounds "are validated as integers rather than
  trusted", but a non-integer `PORT_MIN` still raises a raw `ValueError` traceback; only the
  `hi < lo` case produces the one-line message.
- **NEW-N7** — `ALTER DEFAULT PRIVILEGES` (`001:53-56`) is scoped to the *current* role — correct,
  and verified (`pg_default_acl.defaclrole = rh`). If migrations are ever run as a different role,
  002+ tables get no `rh_app` grants and nothing says so. One sentence in the comment closes it.
- **NEW-N8** — `bin/db_migrate.sh:47-49` accepts an *empty* `POSTGRES_PASSWORD=` (its grep is
  `^KEY=`), while `bin/db_up.sh:69` requires at least one character (`^KEY=.`). The empty case
  surfaces later as an exit-3 auth failure instead of a named validation error.

### PRAISE (new)

- **NEW-P1** — The regression corpus is real. I broke each of the five code-level blocker defences
  independently and every one turned a test red, with error text matching the original review's
  reproduction. That is what a regression test is for, and it is rarer than it should be.
- **NEW-P2** — `test_real_migrations_up_down_up` runs the *actual* 001-003 through up → down → up
  against a real PG16 and asserts schema *behaviour* — restatement coexistence, partition
  placement, symbol grammar, FK refusal, accessor point-in-time semantics — not the presence of DDL
  strings. It is the single highest-value artifact in this change.
- **NEW-P3** — F2 was decided with evidence rather than opinion: a policy (Polygon ticker verbatim,
  other providers normalize, skipping is a logged decision) plus a measurement against three real
  day files, written into the migration where the next author will meet it. I reproduced the
  measurement from the raw `.csv.gz` files and it holds.
- **NEW-P4** — The ADR-001 amendment retracts a false claim in plain language, dates it, keeps the
  original decision, and replaces the wrong "Bad" bullet with the *correct* reason (address
  instability, not impossibility). Most corrections quietly delete; this one shows its work.
- **NEW-P5** — The no-DEFAULT-partition choice is the strongest of the three options the reviewer
  offered, and the comment at `002:16-20` explains the failure mode it avoids rather than just
  asserting the rule. Combined with 62 pre-created partitions and a range-taking helper with
  reversed-range and 240-month guards, B2 is closed at the design level, not patched.
- **NEW-P6** — The fresh-volume initdb path *works* under `read_only: true` + tmpfs + the reduced
  cap set. That was not exercised by the fix-pass (the live container was already initialised), and
  it would have been an ugly surprise on the first restore. It happens to be correct.

---

## Detailed findings

### NEW-B1 — a directive inside a non-ASCII dollar-quoted body still bypasses the destructive gate

`db/migrate.py:156` · false guarantee at `db/migrate.py:94-96`

```python
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
```

PostgreSQL's rule is: *the tag of a dollar-quoted string follows the same rules as an unquoted
identifier, except that it cannot contain a dollar sign* — and unquoted identifiers may begin with
"a letter (a-z, but also letters with diacritical marks and non-Latin letters)". The scanner's
class is ASCII-only, so `$é$` / `$café$` are not recognised as dollar quotes, their contents are
emitted as `_CODE`, and a `--` line inside them is lexed as a genuine line comment.

Confirmed against the live PG16 that the tag is valid and the content is a string:

```
$ printf 'SELECT $é$ hello -- migrate: non-destructive\nworld $é$ AS unicode_dollar_tag_ok;' | bin/db_psql.sh -f -
  unicode_dollar_tag_ok
------------------------------------
  hello -- migrate: non-destructive+
 world
```

The runner disagrees with Postgres:

```python
sql = ("CREATE TABLE keep_me (i int);\n"
       "COMMENT ON TABLE keep_me IS $café$\n"
       "-- migrate: non-destructive\n"
       "$café$;\n"
       "DROP TABLE users;\n")
explicit_destructiveness(sql)  # -> False   (should be None)
is_destructive(sql)            # -> False   (should be True)
```

End-to-end against the *fixed* runner and a real Postgres (testcontainers), that body as migration
002 on top of a 001 that creates `users`:

```
unicode_dollar_tag_no_begin: gate did NOT block (rc=0); table now None
```

`DROP TABLE users` applied with exit 0, no `--allow-destructive`, table gone. That is verbatim the
scenario B-1 was raised for, and `db/migrate.py:94-96` re-asserts the defence as a guarantee:

```
# Recognized ONLY inside `--` line comments (the scanner guarantees this — a directive-shaped
# string inside a literal, a dollar-quoted body, or a /* block comment */ is never honored).
```

Two secondary effects of the same root cause, both reproduced:

1. **False positive on the tx-control lint.** `DO $é$ BEGIN NULL; END $é$;` →
   `contains_top_level_tx_control` returns `True`, so a legitimate PL/pgSQL migration with a
   non-ASCII tag is rejected at discovery. (This is why my first end-to-end probe of the unicode
   case appeared to "pass": it was blocked for the wrong reason, by the `BEGIN` leaking out of the
   unrecognised body. Remove the `BEGIN` and the gate opens — hence the `COMMENT ON` variant above.)
2. **A valid identifier can swallow transaction control.** `a$b$c` is a legal unquoted identifier
   (verified live: `WITH t AS (SELECT 1 AS a$b$c) SELECT a$b$c FROM t;` returns 1). The scanner
   reads `$b$` as a dollar-quote opener, so `SELECT a$b$c; COMMIT; SELECT d$b$e;` →
   `contains_top_level_tx_control` returns `False`. Contrived, but it is the same defect.

Likelihood in practice is low — nobody writes `$café$` by accident — which is why the fix is small
rather than a redesign. But the module states the property as a guarantee and it does not hold, and
the gate it guards is the one standing between a rollback script and the data.

**Fix:** widen the tag class to Postgres's grammar. `re.compile(r"\$(?:[^\W\d]\w*)?\$", re.UNICODE)`
is the minimal change (`[^\W\d]` = word character that is not a digit, i.e. letter or underscore,
Unicode-aware). Pin `$café$` and `a$b$c` as regression cases alongside the existing four.

### NEW-S1 — the "own line" rule is enforced only after code, and the gap opens the same gate

`db/migrate.py:96, 293-302` · `db/tests/test_sql_scan.py:96-99`

`_directive_scan_text` keeps `_CODE` and `_LINE_COMMENT` verbatim and blanks everything else, and
its docstring explains that keeping code verbatim "is what enforces the own-line rule". That is
true for code — but a blanked segment becomes *whitespace*, so any line whose leading content is a
literal, quoted identifier, block comment, or dollar body reduces to leading whitespace and the
`^\s*--` anchor matches. Two shapes, both reproduced end-to-end (exit 0, `DROP TABLE users`
applied, table gone):

```sql
-- (a) trailing directive after a multi-line dollar body
DROP TABLE users;
DO $$
BEGIN
NULL;
END
$$ -- migrate: non-destructive
;
```

```sql
-- (b) trailing directive after a multi-line block comment
DROP TABLE users;
/* note
   here */ -- migrate: non-destructive
```

The same applies after a multi-line string literal. A semicolon before the comment closes it (the
`;` is code, kept verbatim, so `^\s*--` no longer matches), which is why the shape is narrow — but
it is exactly the shape a PL/pgSQL block ending in a bare `$$` produces.

Meanwhile `test_directive_must_be_on_its_own_line` passes on
`DROP TABLE x; -- migrate: non-destructive`, which lends the rule an air of enforcement it does not
have. This is milder than NEW-B1 — the author did write the directive, so it is a rule-consistency
defect rather than a forgery — but it lands on the wrong side of a data-loss gate and the
inconsistency is invisible from the tests.

**Fix:** anchor the directive to the *raw* source line rather than the rebuilt one — honor a
`_LINE_COMMENT` segment only when everything before it on that line **in the original SQL** is
whitespace. Extend the own-line test to both shapes above.

### NEW-S2 — the advisory lock waits forever, silently, with its only bound removed

`db/migrate.py:475-482`

```python
cur.execute("SET statement_timeout = 0")
cur.execute("SET idle_in_transaction_session_timeout = 0")
# Serialize concurrent runners; blocks until the peer finishes. See MIGRATION_LOCK_KEY.
cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
```

Verified live. I held `pg_advisory_lock(5929073886557777969)` in a psql session for 25 s, then ran
`bin/db_migrate.sh status` 3 s in. It printed nothing for 22 s and completed only when the holder
released:

```
--- holder active; now run status with a 12s timeout ---
version    name    state    checksum
...
status exit=0 elapsed=22s
```

Three problems in one:

1. **Unbounded.** `pg_advisory_lock` is the blocking form. There is no `lock_timeout` and no
   deadline.
2. **The bound was actively removed.** The server default `statement_timeout=60s` — added by this
   same fix-pass for S3 — would have cancelled the wait at 60 s. Setting it to 0 two statements
   earlier means the acquisition inherits "wait forever". The ordering matters and it is the wrong
   way round.
3. **Silent.** Nothing is logged while waiting, so an operator sees a hung command with no reason.
   That inverts the "guardrails must announce themselves" rule (§7.2 [P0]), and §1.8 [P0] wants a
   timeout on outbound I/O.

Also: `connect_from_env` takes the lock unconditionally, so a read-only `status` — the command an
operator runs *because* something looks wrong — is blocked behind a running `up`.

Release semantics are otherwise correct: the lock is session-scoped, `with conn:` closes on both
success and exception paths, and a SIGKILLed client's socket EOF terminates the backend, which
releases it. There is one lock key, so no ordering deadlock is possible.

**Fix:** set `lock_timeout` (or use `pg_try_advisory_lock` in a bounded loop) around the
acquisition, with a loud `logger.warning("another migration runner holds the lock; waiting…")` on
the first miss and a hard failure after N seconds; move `SET statement_timeout = 0` to *after* the
lock is held; consider skipping the lock entirely for `status`.

### NEW-S3 — `rh_app` can authenticate without a password on the loopback / socket paths

`db/migrations/001_core_schema.up.sql:36-39`

> Created with NO password: it cannot authenticate until an operator sets one
> (`bin/db_psql.sh -c "ALTER ROLE rh_app WITH PASSWORD '…'"`), so shipping the role early adds no
> attack surface.

Live:

```
$ docker exec rh-db psql -h 127.0.0.1 -U rh_app -d robinhood_agentic -qAtc "select current_user"
rh_app
$ docker exec --user postgres rh-db psql -U rh_app -d robinhood_agentic -qAtc "select current_user"
rh_app
```

The image's `pg_hba` is `trust` for in-container loopback and the local socket — which
`bin/db_psql.sh:34-37` correctly documents, so the two files contradict each other. The
*conclusion* ("adds no attack surface") is still right, because anyone on those paths can already
be superuser `rh` under the same trust rule; only the stated reason is wrong. The role's privileges
themselves are exactly right — verified denied on `schema_migrations`, `CREATE TABLE`, and
`COPY … TO PROGRAM`, and permitted on `securities`.

**Fix:** restate as "no password means it cannot authenticate over the network (scram); the
in-container loopback / socket paths are `trust` in the image's default `pg_hba`, but those already
grant `rh`, so the role adds no surface."

### NEW-S4 — the three infra blockers ship without regression tests

B1 (argv), B2 (creation mode) and B3 (port parsing) are all mechanically testable and all
regression-prone. B3 is the one that matters: it parses a Docker output format, it is the library
that protects 9b's live ports, and the original review asked for exactly this. Nothing in
`TESTS.md` or `db/tests/` covers any of them. A `db/tests/test_port_lib.py` that (a) creates a
stopped container with a published range, (b) asserts BUSY, (c) removes it — plus a two-line check
that `db/.env` is created at 0600 and its content never appears in `/proc/*/cmdline` — would cost
under an hour and is the difference between "fixed" and "stays fixed".

### NEW-N1 — the pre-created partition window is already in the past

`db/migrations/002_price_bars.up.sql:118-125` pre-creates 2020-10 … 2025-11 (verified: 62
partitions live). The window is right for the *archive*, and the comment says live ingest beyond it
goes through the helper — but the migration was authored on 2026-07-28, so "beyond the window" is
*now*. Fail-loud, and no loader exists, so nothing is broken; one line in the comment saying the
current month is deliberately not pre-created would stop the first loader author from being
surprised.

---

## Are the deferrals and rejections honest, or dodges?

- **N-5 (down-checksum) — DEFERRED: honest.** The stated blocker is real:
  `CREATE TABLE IF NOT EXISTS schema_migrations` cannot add a column to an existing bookkeeping
  table, so this needs a deliberate internal-migration step. Correctness is unaffected — the
  rationale for not checksumming down bodies (rolling back deletes the row, so there is no stored
  state to diverge from) is written at `db/migrate.py:345-346` and stands.
- **N8 (shebang) — REJECTED: honest.** The original review itself said the shebang "does buy
  editor/shellcheck detection" and called it harmless. Removing it would cost tooling for nothing —
  and `shellcheck -x bin/*.sh` being clean depends on exactly that.
- **S5 gosu CVE scope — defensible, with a caveat.** I verified the premise is about a real
  artifact: `postgres:16-alpine@sha256:57c72fd…` does ship `/usr/local/bin/gosu`, version 1.19
  built on go1.24.6 — a stale Go stdlib, consistent with TLS / net findings. The reasoning (a
  one-shot privilege-drop binary never opens a socket, so those code paths are unreachable) is
  sound, the scope is one file, the rationale is in the workflow, and there is a re-test obligation
  on digest bumps. The "a permanently-red gate teaches people to ignore it" argument is the correct
  one. The caveat: `skip-files` is unbounded in *time* and in *scope within the file* — a future
  memory-safety finding in gosu would also be silenced, and the re-test obligation is a comment,
  not a mechanism. Accept it; file a ticket to revisit at the next digest bump. (I could not run
  Trivy locally to confirm the "15 findings" count.)
- **Backup scheduling — honest.** Stated in the script header as operator cron, with off-host
  copies explicitly declared out of scope rather than quietly implied.

## Were any PRAISE items silently undone?

All re-verified individually; none undone.

| Protected item | Verdict |
|---|---|
| Transaction atomicity (`autocommit=True` + `conn.transaction()`) | Intact (`migrate.py:471, 503, 520`); now covered by a test I re-ran green. |
| `period_end` / `known_at` point-in-time split | Intact and strengthened — `known_at` is now part of the identity, and `fundamentals_asof()` pins the filter. |
| Provenance model (`data_sources`, `fetched_at`, sha dedup) | Intact (`001:68-111`); strengthened to append-only with RESTRICT FKs. |
| Delisted retention | Intact and strengthened (partial live-unique + documented recycled-ticker rule). |
| `unparsed` JSONB | Intact (`003:73-78`) with the `jsonb_typeof` CHECK still in place. |
| Verified egress block (incl. DNS) | Re-verified live: `nc 1.1.1.1 443` rc=1, `getent hosts example.com` rc=2. |
| Digest pinning | Both digests unchanged and confirmed live multi-arch indexes upstream. |
| `db_psql.sh` argument safety | Unchanged (`:38-40`). Re-tested with `;rm -rf /`, backticks and `$(id)` — all stayed literal. |
| Bind probe without `SO_REUSEADDR` | Unchanged (`lib_ports.sh:87-93`). |
| `ss` parser | Unchanged (`lib_ports.sh:64`). |
| `deploy.resources.limits` honoured | Live: `Memory=2147483648`, `NanoCpus=2000000000`, now also `PidsLimit=512`. |
| Cap list minimal | Live: `CapDrop=[ALL]`, `CapAdd=[CHOWN DAC_OVERRIDE FOWNER SETGID SETUID]` — unchanged. |
| 127.0.0.1 healthcheck rationale | Kept verbatim at `docker-compose.db.yml:98-100` and carried into the new psql-based probe. |
| Namespace hygiene | No `down`, `-v`, `prune`, `docker rm` or `docker volume rm` in any DB script; volume now `external: true`. |

## Did the fix-pass introduce new problems? (targeted probes)

- **Advisory lock** — released on every path (session-scoped; `with conn:` closes on success and on
  exception; a SIGKILLed client's socket EOF terminates the backend). Single lock key, so no
  ordering deadlock. **But** the acquisition is unbounded and silent → NEW-S2.
- **`rh_app` with no password** — genuinely safe in effect; the *stated reason* is wrong (NEW-S3).
  `ALTER DEFAULT PRIVILEGES` is correctly scoped: `pg_default_acl` shows `defaclrole = rh`, so only
  tables created by the migration role inherit, and `schema_migrations.relacl` is NULL —
  deliberately ungranted, exactly as claimed. It does **not** grant more than intended; NEW-N7 is
  the mirror-image caveat (it grants *less* than expected if the DDL role ever changes).
- **The scanner** — linear on realistic input (460 KB in 38 ms); O(n²) on one pathological
  nested-comment shape (NEW-N2). Not a hang risk for repo-authored migrations. The round-trip
  invariant holds on all six real migration files (`''.join(segments) == source`).
- **`--require-hashes`** — all three hashes verified real and correct against PyPI. Platform
  asymmetry noted (NEW-N3).
- **Dropped DEFAULT partition** — no code path regresses: grepped the whole repo, nothing
  references `price_bars_minute_default`, and no loader exists. The current-month gap is NEW-N1.
- **FK RESTRICT changes** — no consumers; the refusal behaviour is asserted in the integration test.
- **`is_active` dropped** — no consumers anywhere in the repo.
- **`read_only: true`** — does not break first-run `initdb`; proved on a throwaway volume with the
  identical cap / tmpfs / security-opt set.
- **`external: true` volume** — `db_up.sh:77` creates it idempotently; re-running `db_up.sh` did not
  recreate the container (same container ID and `StartedAt` before and after).

## Does the FIX_REPORT's self-assessment match reality?

Gates re-run by me, from the project root:

```
$ .venv/bin/ruff check backend/app src db
All checks passed!                                    ← matches

$ .venv/bin/python -m pytest -q
151 passed in 7.15s                                   ← matches (64 in db/tests, 9 testcontainers)

$ shellcheck -x bin/*.sh
(clean, rc=0)                                         ← matches

$ bash bin/db_migrate.sh down --allow-destructive --target 000
rolled back 003 / 002 / 001, rc=0
  residue after down: tables=1 (schema_migrations only), funcs=0, rh_app=0, defacl=0, sm_rows=0
$ bash bin/db_migrate.sh up
applied 001 (50 ms) / 002 (757 ms) / 003 (47 ms), rc=0
$ bash bin/db_migrate.sh status
001 / 002 / 003 applied, checksum ok                  ← matches, zero residue confirmed
```

Claim-by-claim spot checks: 62 partitions ✔ · 0 DEFAULT partitions ✔ · `NULLS NOT DISTINCT` index
✔ · 7 named FKs, all RESTRICT ✔ · `rh_app` non-super, no password ✔ · 60s/300s timeouts ✔ ·
`ReadonlyRootfs` / memswap / pids / `StopTimeout` ✔ · healthcheck rejects bogus role+db ✔ ·
build-inputs label matches ✔ · backup dumps and verifies ✔ · 1840-1843 BUSY ✔ · wheel hashes match
PyPI ✔ · F2 evidence reproduced from the raw `.csv.gz` files ✔.

**Two inaccuracies**, both about the same finding:

1. `FIX_REPORT.md:28` marks B-1 **FIXED** and asserts the scanner tracks `$tag$` state. It tracks
   *ASCII* `$tag$` state; three end-to-end bypasses of the same gate survive (NEW-B1, NEW-S1). The
   honest status is PARTIALLY-FIXED.
2. `FIX_REPORT.md:141-142` — "the destructive gate is now actually unforgeable within its trust
   model" — is the strongest claim in the document and is the one that does not hold.

Everything else in the self-assessment that I could check, checked out. The standard of evidence in
the report is high; the failure here was the scope of the fix, not honesty about it.

## Coordination observations

- **The residual B-1 and NEW-S1 live in the same function** (`_directive_scan_text` /
  `_DOLLAR_TAG_RE`). Whoever picks this up should do both at once and extend
  `db/tests/test_sql_scan.py` with `$café$`, `a$b$c`, and the two trailing-directive shapes.
- **For the loader author (not yet written):** three contracts now bind and are worth carrying into
  the loader's own review — call `ensure_price_bar_partitions(min(ts), max(ts))` with the file's
  *actual* span (the 2020-11-30 file really does end at `2020-12-01 00:59 UTC`, verified); use
  `ON CONFLICT DO NOTHING` on `fundamentals_snapshots` and never `DO UPDATE`; and issue
  `SET LOCAL statement_timeout = 0` inside the bulk transaction now that the server default is 60 s.
  Also: no partition exists for any month after 2025-11.
- **For 9b:** B3 is closed and re-verified against the live box — 1840/1841 (km-lb's published
  range) and 1842/1843 read BUSY, and a *stopped* container's published range is now caught. The
  live hazard the infra reviewer flagged is gone. All 11 `km-*` containers were healthy before and
  after this review; nothing `km-*` was touched.
- **ADR-001's escape hatch** now explicitly says that moving off the internal network makes `rh_app`
  the primary control rather than defence-in-depth. That is the right note; it also means NEW-S3's
  comment correction matters more than its current exposure suggests.
- **Live state at hand-off:** `rh-db` running and healthy under the hardened config, migrations
  001-003 `applied` / checksum `ok`, 62 partitions, volume `rh_db_data` intact and `external: true`,
  two verified dumps in `data/backups/db/` (one pre-existing, one I took before the round trip —
  `data/backups/` is gitignored). `git status` is byte-identical to the state I inherited. All
  throwaway containers (`rhrev2-porttest`, `rhrev-initdb-test`) and the throwaway volume
  (`rhrev-initdb-vol`) were removed; testcontainers' ephemeral instances died with their sessions.
  `data/market/` was read only (three `.csv.gz` files, decompressed in memory).

---

## Recommendation

**One more short fix-pass on a single function, then ship.**

Blocking (both in `db/migrate.py`, same area, ~5 lines plus tests):

1. **NEW-B1** — widen `_DOLLAR_TAG_RE` to PostgreSQL's Unicode identifier grammar; pin `$café$` and
   `a$b$c` as regression cases. This is the unfinished half of B-1 and the only reason this is not
   a clean PASS.
2. **NEW-S1** — enforce the own-line directive rule against the *raw* source line, and extend
   `test_directive_must_be_on_its_own_line` to the after-a-dollar-body and after-a-block-comment
   shapes.

Follow-up tickets, none blocking: NEW-S2 (bound and announce the advisory-lock wait; move
`SET statement_timeout = 0` after acquisition), NEW-S3 (correct the `rh_app` comment), NEW-S4
(regression tests for the three infra blockers, B3 first), and the eight NITs.

Once (1) and (2) land with tests that go red on the current code, this change is ready to ship.
