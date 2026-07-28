# Review: migration runner (db/migrate.py)

Reviewer: independent senior review (fixpass). Scope: `/home/jared-williams/projects/3b. Robinhood Agentic/db/migrate.py` only.
Method: full read + empirical verification. Pure-function hypotheses were executed against the real module
(psycopg stubbed); transaction-semantics hypotheses were executed inside the actual `rh-migrate:local`
container against the live `rh-db` (scratch tables only, all dropped; `status` re-verified clean afterward:
001–003 applied, checksums ok).

## Summary verdict: REQUEST CHANGES

The core machinery is genuinely sound — the central atomicity guarantee (migration body + bookkeeping row in
one transaction, on an autocommit connection via `conn.transaction()`) was verified empirically and holds,
including for multi-statement bodies. But the SQL-text analysis layer that feeds both safety gates
(transaction-control rejection and the destructive gate) is built from order-dependent regex passes rather
than a single tokenizing scan, and I demonstrated four distinct fail-open breaks in it — including one that
lets a `DROP TABLE` migration sail past `--allow-destructive` while the module's own docstring claims that
exact forgery is impossible. Combined with the total absence of tests for a data-integrity-critical,
deliberately-testable module, this is REQUEST CHANGES, not a rubber stamp. All blockers are cheaply fixable;
nothing here requires redesign.

## Bar checklist

| Bar item | Verdict | Note |
|---|---|---|
| §0 [P0] Robust by default (I/O failure handled) | PASS (partial) | Clean error taxonomy + exit codes; missing `connect_timeout` (see SF-3) |
| §0 [P0] Fail closed, fail loud | **FAIL** | Stripping layer fails OPEN in four demonstrated cases (B-1) |
| §0 [P0] Clean tree (no dead code/debug/secrets) | PASS | `print()` is legitimate CLI output, not debug residue |
| §1.2 [P0] Full type hints on every signature | PASS | `bool | None`, `list[str] | None`, dataclass typed; no `Any` |
| §1.8 [P0] Explicit timeout on every outbound I/O call | **FAIL** | `psycopg.connect` at :266 has no `connect_timeout` (SF-3) |
| §1.9 Logging | PASS (contextual) | Plain `logging` is defensible for a CLI runner; structlog N/A |
| §4.5 [P0] Both directions required + tested | PARTIAL | Pairing enforced (discovery); "tested in CI" — no tests exist (B-2) |
| §4.5 [P0] Never edit an applied migration | PASS | Checksum-before-plan at :352-358, verified live |
| §4.5 [P0] Destructive changes gated | **FAIL** | Gate exists and fires (verified, exit 1) but is forgeable (B-1) |
| §4.5 lock_timeout/statement_timeout on migration conn | N/A (documented deviation) | `statement_timeout=0` is deliberate + justified in docstring :25-26; acceptable for a private single-writer DB |
| §4.6 Explicit transaction boundaries | PASS | Verified empirically (see Detailed B-0 note under Praise) |
| §4.7 [P0] Parameterized queries | PASS | All bookkeeping writes parameterized (:297-301, :314); interpolation only of trusted repo file content, by design |
| §5.2/§1.10 [P0] Tests exist, unhappy paths covered | **FAIL** | No `db/tests/` at all (B-2) |
| §7.2 [P0] Guardrails loud, never silent | PARTIAL | DestructiveBlocked is loud (verified); `down --target 1` silently no-ops (SF-1) |

## Findings

### BLOCKER
- **B-1** — The SQL stripping layer fails open: four demonstrated bypasses of the two safety gates, the worst
  being a destructive migration silently downgraded past `--allow-destructive` by a directive inside a
  dollar-quoted body. `db/migrate.py:114-170`.
- **B-2** — Zero tests for the runner. No `db/tests/` exists. The module advertises importable-for-tests
  design (`db/migrate.py:5-6`) and its analysis layer is exactly where an hour of unit probing found B-1's
  four instances. Bar §1.10/§5.2 [P0].

### SHOULD-FIX
- **SF-1** — `--target` is compared lexically with no validation; an unpadded target over-applies on `up`
  (`--target 2` applies 003) and silently no-ops on `down`. `db/migrate.py:361-362, :406`.
- **SF-2** — No advisory lock; two concurrent runners degrade to confusing mid-deploy SQL errors (exit 2)
  instead of clean serialization. `db/migrate.py:259-272`.
- **SF-3** — No `connect_timeout` on `psycopg.connect` (:266); an unreachable-but-routable host hangs the
  runner indefinitely. Bar §1.8 [P0]. Also: a `psycopg.Error` from the two `SET` statements (:270-271) leaks
  the connection (no close on that path) — cosmetic since the process exits, but one `try` fixes both.
- **SF-4** — Migration bodies are re-read from disk on every property access; the SQL that executes at
  :293/:310 is not literally the text validated at :234-241 (TOCTOU). Mitigated today by the `:ro` repo
  mount in `bin/db_migrate.sh:56`; cache the text in the dataclass to close it structurally.
- **SF-5** — Mixed version widths are warned about only in a comment (:57-58) but not enforced;
  `sorted(["999","1000"])` → `["1000","999"]`, silently wrong apply order. Discovery should reject
  mixed-width sets — one `len(set(len(v) for v in pairs)) > 1` check.
- **SF-6** — argparse usage errors exit 2, colliding with the documented "2 = SQL execution failure"
  (:29, verified live: `frobnicate` → exit 2). A deploy script branching on exit code misreads a typo as a
  SQL failure. Subclass `ArgumentParser.error`/`exit` or renumber.

### NIT
- **N-1** — `finally: conn.close()` (:488) is redundant: psycopg3's `with conn:` closes the connection on
  both success and exception paths (verified empirically), and `close()` is idempotent. Harmless; either
  drop the `with conn:` or the `finally`, with a comment, so the next reader doesn't puzzle over ownership.
- **N-2** — `--dry-run` help text says "never opens a write transaction" (:439), but `ensure_bookkeeping`
  (:347, :396) will CREATE the `schema_migrations` table on a fresh database even under `--dry-run`.
  Behavior is fine; the claim is falsifiable — fix the help text or skip DDL on dry-run.
- **N-3** — Nested block comments (valid Postgres) false-positive the tx-control check
  (`/* outer /* inner */ COMMIT stray */` → rejected; verified). Fail-safe direction, so merely annoying.
  Same for quoted identifiers: `CREATE TABLE t ("begin" int)` is rejected (verified). Both worth a line in
  the TxControlInMigration error message ("if this is a false positive, rename/rephrase").
- **N-4** — Directives are only recognized in `--` line comments at line start; `/* migrate: destructive */`
  is silently ignored (verified, falls through to the sniff — safe direction). Document it in the
  DIRECTIVE_RE comment (:72) so an author doesn't believe they've declared something they haven't.
- **N-5** — UP-only checksum: an edited DOWN file after apply is undetectable, so the rollback that runs is
  not the rollback that was reviewed with its up. The stated rationale (:191-195) is defensible — blocking
  down-edits would prevent fixing a broken down script before rollback, which is precisely when you need to
  edit it — but it addresses stored-state divergence, not review-provenance. Suggest storing a
  `down_checksum` column informationally (warn on mismatch in `status`, never block).
- **N-6** — Orphan applied rows (row in `schema_migrations`, file gone) are surfaced by `status` (:332-336,
  good) but silently ignored by `up`/`down`. A deploy that only runs `up` never sees the ORPHAN warning.
- **N-7** — No escape hatch for statements that cannot run inside a transaction (`CREATE INDEX
  CONCURRENTLY`, bar §4.4); such a migration will always fail inside `conn.transaction()`. Fine for this
  database's size today — document the limitation in the module docstring so the first person who needs CIC
  learns it from the doc, not from a failed deploy.

### PRAISE
- **P-1** — The central guarantee is real and verified. `autocommit=True` + `with conn.transaction():` is
  the correct psycopg3 idiom: autocommit means "no implicit transaction," and `Connection.transaction()`
  opens an explicit one regardless. Empirically confirmed in the actual runner container against rh-db:
  a raised exception inside the block rolled back both DDL and DML; a multi-statement body
  (`CREATE TABLE; INSERT; SELECT 1/0`) rolled back completely. A partially-applied migration is impossible
  as documented (:13-14). Do not "simplify" the autocommit flag away — without it, psycopg3 would wrap the
  session-scoped `SET statement_timeout` (:270) in an implicit transaction and the design breaks subtly.
- **P-2** — Checksum-verification-before-planning in `cmd_up` (:350-358) checks *every* applied migration,
  not just pending ones, and the error message tells the operator exactly what to do. Verified live: edited
  applied files halt the run before any SQL executes.
- **P-3** — Validation of both directions of every pair at discovery time (:232-241), before the database is
  touched, with the tx-control lint motivated by a real production bug (9b ADR-013). Fail-fast ordering in
  `main` (discover → connect → execute) is exactly right.
- **P-4** — The destructive gate evaluated at plan time including `--dry-run` (:367-375), and *all* rollbacks
  gated unconditionally (:411-417) — verified live, loud message, exit 1. This satisfies the project's
  "guardrails must announce themselves" standing order.
- **P-5** — Small correctness touches that most hand-rolled runners miss: server-side `clock_timestamp()`
  for duration (immune to host clock skew), parameterized bookkeeping writes, `applied_by` principal capture
  with a container-safe fallback (:248-256), zero-padded lexical-sort invariant called out with its failure
  mode (:57-58), and the actionable psycopg import guard (:43-47).

## Detailed findings

### B-1 — Stripping layer fails open; destructive gate is forgeable (`db/migrate.py:114-170`)

The three stripping functions apply regexes in a fixed sequence. Because no pass knows about the tokens the
other passes consume, text can be classified as "comment" when it is code and "code" when it is a literal.
Four instances, all reproduced by executing the module's own functions:

1. **Directive inside a dollar-quoted body downgrades a destructive migration.**
   `_strip_string_literals_only` (:134-140) strips only single-quoted literals — not dollar-quoted ones. Its
   own docstring claims "a literal containing the text `-- migrate: non-destructive` cannot forge a
   directive"; that claim is false for `$$…$$`. Reproduced:

   ```sql
   DROP TABLE users;
   DO $$
   BEGIN
   -- migrate: non-destructive
   NULL;
   END
   $$;
   ```

   `explicit_destructiveness()` returns `False` (declared non-destructive), so `is_destructive()` returns
   `False` and this `DROP TABLE` applies **without** `--allow-destructive`. The directive is the highest-
   precedence mechanism in the gate (:167-169), so forging it defeats everything below it. Under the trust
   model (repo-authored, human-reviewed files) the adversarial path is remote, but the accidental one is not:
   PL/pgSQL bodies are full of `--` comments, and a comment *about* the directive syntax inside a DO block is
   enough. A data-loss gate whose documented defense is demonstrably false is broken-by-construction.
   **Fix:** strip dollar-quoted strings in `_strip_string_literals_only` too (the regex already exists at
   :128), or better, fix the root cause per below.

2. **`--` inside a single-quoted literal swallows real SQL — destructive-sniff false negative.**
   `strip_sql_comments` (:114-118) removes `--[^\n]*` without knowing about literals, so
   `INSERT INTO t VALUES ('a--b'); DROP TABLE users;` has everything from `--b'` onward deleted before the
   sniff runs — `is_destructive()` returns `False`. This directly falsifies the documented asymmetry at
   :164-166 ("erring toward false positives is the correct asymmetry"): the implementation errs toward false
   *negatives* whenever a literal contains `--`.

3. **Same shape for the tx-control check.** `SELECT 'a--b'; COMMIT;` → `contains_top_level_tx_control()`
   returns `False`. The "runner owns the transaction" lint (:15-19), the module's headline guarantee against
   the 9b ADR-013 bug, is bypassed by a literal containing `--` on the COMMIT's line. Also `/*` inside a
   literal swallows everything to the next `*/` (reproduced with `is_destructive`, same mechanism applies
   here).

4. **`$$` inside two line comments swallows the code between them.** `strip_sql_noise` (:121-131) strips
   dollar-quotes *before* comments, so:

   ```sql
   SELECT 1;
   -- a $$ b
   COMMIT;
   -- c $$ d
   SELECT 2;
   ```

   The two `$$` tokens inside comments pair up, deleting `COMMIT;` — `contains_top_level_tx_control()`
   returns `False` (reproduced). Dollar signs in comments ("cost: $$") are not exotic.

**Root cause and fix:** sequential context-free regex passes cannot correctly separate comments from literals
from code, because each token type can contain the delimiters of the others. The correct implementation is
one left-to-right scanner (~30 lines, still stdlib-only) that walks the text once tracking state
(`code | line_comment | block_comment(depth) | squote | dollar(tag)`) and emits code/comments/literals as
requested. That single function replaces all three strippers, fixes all four instances plus nested block
comments (N-3), and is trivially unit-testable — which feeds directly into B-2.

### B-2 — No tests (`db/` has no tests directory; nothing imports `migrate`)

There is no `db/tests/` and no test anywhere in the repo exercising this module. This is the project's
highest-risk artifact: it executes arbitrary DDL against the system of record and is the sole enforcement
point for the destructive gate and the checksum invariant. The module was explicitly designed for
testability — `main(argv)` importable (:5-6, :445), pure functions with no I/O (:114-170), discovery
separated from execution (:199-244) — and then no tests were written against that surface. Every defect in
B-1 and SF-1 is reachable by a pure unit test with no database; the transaction-atomicity and exit-code
contracts are reachable with testcontainers per bar §5.4. Bar §1.10 [P0] makes pytest+coverage a blocking
gate; a brand-new module at 0% cannot pass it. Minimum bar for approval: unit tests for the (rewritten)
scanner, `is_destructive`/`explicit_destructiveness` (incl. the B-1 corpus above as regression cases —
§5.2 [P0]: every bug fix ships with a regression test), `discover_migrations` error taxonomy, `--target`
validation, and exit-code mapping via `main(argv)` with `DATABASE_URL` pointed at a testcontainer. Register
the suite in `TESTS.md` per the project standing order.

### SF-1 — `--target` lexical comparison with no validation (`db/migrate.py:361-362, :406`)

Versions are zero-padded strings and `--target` is a raw user string; both comparisons are lexical.
Reproduced against the module's own logic with versions `001,002,003`:

- `up --target 2` applies **all three** (`"003" <= "2"` lexically) — over-applies past the operator's
  intended stopping point. The destructive gate still applies to the over-applied set, but a non-destructive
  migration the operator explicitly excluded gets applied silently.
- `up --target 1` likewise applies all three.
- `down --target 1` rolls back **nothing** and logs "nothing to roll back above target 1" — a silent no-op
  where the operator asked for a rollback (violates the project's "guardrails/tools never silently
  no-op" posture).

**Fix:** validate `--target` in `main` before connecting: require `re.fullmatch(r"\d{3,}", target)` and
require membership in the discovered version set (`up`) or `{discovered} ∪ {"000"}`-style sentinel (`down`);
exit 1 with a message naming valid targets otherwise.

### SF-2 — No inter-runner lock (`db/migrate.py:259-272`)

Two concurrent `up` runs both read `applied_migrations`, both plan the same pending set, and race. The
design's atomicity does contain the damage — the loser's body+bookkeeping roll back together on the
`schema_migrations` PK conflict or on `already exists` DDL errors — so this is not a corruption bug, and
that containment is worth stating. But the loser dies mid-deploy with a misleading "migration failed" at
exit 2, and even `ensure_bookkeeping`'s `CREATE TABLE IF NOT EXISTS` can race (Postgres can still raise a
duplicate-key error on the catalog under concurrency). One statement after connect fixes it:
`SELECT pg_advisory_lock(<constant>)` (session-scoped, released on close), turning the race into clean
serialization. Cheap enough that "single operator today" is not a reason to skip it.

### SF-3 — No connect timeout; connection leak on SET failure (`db/migrate.py:259-272`)

`psycopg.connect(dsn, ...)` (:266) sets no `connect_timeout`; libpq's default is wait-forever. A
blackholed host (container present, DB wedged) hangs the runner — and any deploy script above it —
indefinitely, violating bar §1.8 [P0] (explicit timeout on every outbound I/O call). Additionally, if either
`SET` at :270-271 raises, the freshly opened connection is never closed (the exception propagates out of
`connect_from_env` before `main`'s `finally` owns `conn`). Fix both at once: pass `connect_timeout=10` (or
honor a `MIGRATE_CONNECT_TIMEOUT` env), and wrap the `SET`s in `try: … except: conn.close(); raise`.

### SF-4 — Validated SQL ≠ executed SQL (`db/migrate.py:181-187, :293, :310`)

`up_sql`/`down_sql` are properties that re-read the file on every access. Discovery validates one read
(:234), the destructive gate classifies another (:369, :378), and `apply_one` executes a third (:293) —
plus `checksum` (:196) reads a fourth. If the file changes between reads, the runner executes text that was
never validated or checksummed. Today `bin/db_migrate.sh:56` mounts the repo `:ro`, which mitigates it in
the blessed path — but `migrate.py` is also importable and runnable directly, where no such mount exists.
Read each body once in `discover_migrations` and store the text (and checksum) on the frozen dataclass;
this also drops four redundant file reads per migration.

### SF-5 — Mixed version widths accepted, ordering silently wrong (`db/migrate.py:57-59, :225`)

`FILENAME_RE` accepts `\d{3,}`, and `sorted(pairs)` is lexical: a `1000_x` pair added alongside `999_y`
sorts **before** it (`["1000","999"]`, reproduced), so migrations apply out of order with no error. The
comment at :57-58 says "do not mix widths" — a comment is not enforcement (bar §0: fail closed). Discovery
already has the full version set; reject when `len({len(v) for v in pairs}) > 1` with a message pointing at
the widening procedure the comment describes.

### SF-6 — argparse exit code collides with EXIT_SQL (`db/migrate.py:29, :446`)

`build_parser().parse_args(argv)` calls `sys.exit(2)` on usage errors — verified live: an invalid command
exits 2, which the documented contract (:29) and `bin/db_migrate.sh:15` define as "SQL execution failure."
Any deploy tooling branching on exit codes will misclassify a typo as a mid-migration SQL failure — the one
code that should mean "go look at the database." Either override `ArgumentParser.exit` to map usage errors
to `EXIT_VALIDATION`, or renumber the contract so 2 is unambiguous.

## Coordination observations

For the aggregator and the other reviewers — items outside my file but discovered while verifying it:

- **Fixing B-1 properly (single-pass scanner) changes the module's testable surface** — the fix-pass agent
  should implement scanner + tests together, using my reproduction corpus (all inputs are in this review) as
  the regression set.
- **`bin/db_migrate.sh` (other reviewer's scope) documents `down --allow-destructive --target 001`** — the
  only documented `--target` usage is padded, which is why SF-1 has not bitten yet. Whoever owns the shell
  review may want to note the docs should keep showing padded targets even after validation lands.
- **No `pyproject.toml`/mypy/ruff/CI gate covers `db/`** at all as far as I can see (`db/` contains only
  `Dockerfile`, `migrate.py`, `migrations/`, `__pycache__`). Bar §1.10 [P0] gates are unenforceable until
  the packaging/CI story exists; B-2's tests need a home (`db/tests/` + TESTS.md entry) and a runner
  (host pytest with testcontainers, or a container harness consistent with ADR-001).
- **`__pycache__/` sitting in `db/`** suggests the module has been imported/run from the host at least once
  outside the container path — consistent with SF-4's note that the `:ro`-mount mitigation only covers the
  blessed path. Also worth a `.gitignore` check by whoever owns repo hygiene.
- **Live DB state after this review:** untouched — `status` shows 001–003 applied, checksums ok. My probes
  used throwaway `_probe_*` tables, all dropped; no rollback was performed.
