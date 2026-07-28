# Verification: filename-marked destructive gate (redesign)

Round 4 adversarial verification of ADR-002. Reviewer independent: did not write, review, or fix
any part of rounds 1–3 or the redesign. Method: 90 hostile probes driven through `migrate.main()`
against a throwaway `postgres:16-alpine` (`rh-verify-pg`, host-bound `127.0.0.1:55432`, removed
after), one fresh database per probe, every attack migration in `/tmp` via `--migrations-dir`.
Five defence reverts executed on a scratch copy of `db/`. All declared gates re-run.

---

## Summary verdict

# **FAIL** — scoped

The redesign's **central thesis is validated and should not be re-litigated**. Destructiveness read
from the filename is unforgeable: 20 filename/marker/symlink/path attacks — unicode homoglyphs,
case variants, doubled and misplaced markers, a name *containing* the word "destructive", a symlink
whose link name is unmarked and whose target name is marked — produced **zero** classification
forgeries. The server-side transaction-ownership check survived **22** hijack shapes including
`COMMIT AND CHAIN`, `ABORT`, `END`, `PREPARE TRANSACTION`, a `COMMIT` inside a `CALL`ed procedure,
and a `search_path`-shadowing attempt. Every byte-level hazard is refused before the database is
touched. Discovery is linear. All five revert claims reproduce exactly.

It fails on one layer: **the fail-closed sniff is not fail-closed.** Seven migration bodies
containing a real `DROP TABLE` / `TRUNCATE` applied end-to-end with **exit 0 and no
`--allow-destructive`**, the table gone and the migration recorded. The cheapest is four
characters: `drop/**/table users;`. Three documents state in terms that this cannot happen:

| Location | Claim | Status |
|---|---|---|
| `ADR-002` §Decision 2 | "the sniff cannot be forged into silence" | **REFUTED** |
| `FIX_REPORT.md` §Design 2 | "Cannot produce a false negative from forged content" | **REFUTED** |
| `db/migrate.py:30` | "The sniff can only ever over-fire; it cannot be forged into silence" | **REFUTED** |
| `db/tests/test_runner_db.py:7` | corpus "prove[s] file CONTENTS cannot influence it" | **REFUTED** — see §Regression |

This is a materially smaller failure than rounds 1–3 (which forged the *classification* itself),
and the remediation is local: one regex, one keyword list, three sentences. But an operator reading
any of the three sentences above would believe an unmarked file cannot drop a table, and that
belief is false.

---

## Forgery attempts

Setup for every row: `001` creates `users` with one row; the attack is an **unmarked** `002`, run as
`up` with **no** `--allow-destructive`. FORGED = exit 0 with `users` dropped or emptied.

### A. Sniff evasion — file contents (the failure)

| # | Attack | Shape | Result |
|---|---|---|---|
| 1 | Block comment between keywords | `drop/**/table users;` | **FORGED** — exit 0, `users` gone, 002 recorded |
| 2 | Line comment between keywords | `DROP -- x\nTABLE users;` | **FORGED** — exit 0, `users` gone |
| 3 | Multi-line block comment between keywords | `DROP/* a\nb */TABLE users;` | **FORGED** — exit 0, `users` gone |
| 4 | Dynamic SQL, string concat | `DO $$ BEGIN EXECUTE 'DR'\|\|'OP TABLE users'; END $$;` | **FORGED** — exit 0, `users` gone |
| 5 | Dynamic SQL via `format()` | `EXECUTE format('%s %s %I','DROP','TABLE','users')` | **FORGED** — exit 0, `users` gone |
| 6 | Dynamic SQL from `chr()` | `EXECUTE chr(68)\|\|chr(82)\|\|chr(79)\|\|chr(80)\|\|' TABLE users'` | **FORGED** — exit 0, `users` gone |
| 7 | `TRUNCATE` via dynamic SQL | `EXECUTE 'TRUNC'\|\|'ATE users'` | **FORGED** — exit 0, table emptied |
| 8 | Symlink to a sniff-evading body outside the dir | link `002_atk.up.sql` → `/tmp/…/evil.sql` = shape 1 | **FORGED** — exit 0, `users` gone |
| 9 | `TRUNCATE/**/TABLE` | comment split after `TRUNCATE` | BLOCKED (`TRUNCATE` alone matches) |
| 10 | NBSP / ZWSP / FF / VT / CR / LF between keywords | `DROP TABLE`, `DROP​TABLE`, `DROP\fTABLE`, … | BLOCKED (whitespace forms sniffed; ≥0x80 forms are a PG syntax error → exit 2) |
| 11 | Quoted identifier | `DROP TABLE "users";` | BLOCKED |
| 12 | Control: plain `DROP TABLE users;` | unmarked | BLOCKED, exit 1, table + row intact |
| 13 | Control: marked file, `DROP TABLE users;` | `.destructive.up.sql` | BLOCKED without the flag; applies with it |

Shapes 4–7 are unfixable by any text rule — dynamic SQL is not analysable without executing it.
Shapes 1–3 are cheaply fixable (§Recommendation) and are the ones I weight as the defect: they are
*not* obviously obfuscated to a reviewer skimming a diff.

### B. Destructive verbs absent from the sniff list (unmarked, exit 0, recorded)

| Statement | Applies unmarked | Documented? |
|---|---|---|
| `DELETE FROM users WHERE true;` | yes | yes — `migrate.py:119` |
| `ALTER TABLE users DROP COLUMN i;` | yes | yes — `migrate.py:119` |
| `UPDATE users SET i = NULL;` | yes | implied |
| `DROP OWNED BY <role>;` | yes | **no** — cascades to every object the role owns |
| `DROP MATERIALIZED VIEW mv;` | yes | **no** |
| `DROP VIEW v;` / `DROP ROLE r;` | yes | no (recreatable / low value) |

`DROP OWNED BY` is the realistic one: `001_core_schema` creates the `rh_app` role, so a future
role-management migration writing `DROP OWNED BY rh_app; DROP ROLE rh_app;` sails through unmarked.
That is an *accidental* miss, not an adversarial one.

### C. Filename, marker, and path attacks — 20/20 BLOCKED

| Attack | Shape | Result |
|---|---|---|
| Name contains the marker word | `002_destructive_cleanup.up.sql` + `DROP TABLE` | BLOCKED (sniff; `up_destructive` correctly `False`) |
| Name contains it negated | `002_undestructive_thing.up.sql` | BLOCKED |
| Case variation | `.DESTRUCTIVE.` / `.Destructive.` | BLOCKED — loud discovery error |
| Unicode homoglyph | `.dеstructive.` (Cyrillic е), `.ｄestructive.` (fullwidth) | BLOCKED — loud |
| Doubled marker | `.destructive.destructive.up.sql` | BLOCKED — loud |
| Wrong position | `.up.destructive.sql` | BLOCKED — loud |
| Trailing/leading dot or space in marker | `.destructive..up.sql`, `.destructive .up.sql`, `. destructive.up.sql` | BLOCKED — loud |
| Marked + unmarked same direction | both files present | BLOCKED — `duplicate up file` |
| Symlink, link name unmarked → target name marked | classification follows the link name | BLOCKED (sniff caught the body) |
| `--migrations-dir` with `..` traversal | `…/dir/../dir` | BLOCKED — same gate applies |
| Hidden dotfile | `.002_atk.up.sql` with `DROP TABLE` | BLOCKED — loud (it is a `.sql` file) |
| Filename with trailing newline | `002_x.up.sql\n` | Silently skipped (see NIT-1 — `FILENAME_RE` *does* match it) |
| TOCTOU: file swapped mid-execution | benign at discovery → `DROP TABLE` after 0.4 s, body `pg_sleep(2)` | BLOCKED — cached text executed; the swap had no effect |
| `--target` includes a destructive migration | `up --target 003` with destructive `002` pending | BLOCKED, exit 1 |
| `--target` on the destructive version itself | `up --target 002` | BLOCKED, exit 1 |
| `--dry-run` + `--target` | `up --dry-run --target 002` | BLOCKED, exit 1 |
| `down` without the flag | marked and unmarked downs | BLOCKED, exit 1 |
| `down --target 000` without the flag | full teardown | BLOCKED, exit 1 |
| `down --dry-run` without the flag | plan-only rollback | BLOCKED, exit 1 |

**No attack changed a file's classification.** Every forgery in §A is a *detection* failure in the
backstop, never a *classification* forgery. That distinction is the redesign's whole point, and it
holds.

---

## Transaction-status attacks

Body shape: `CREATE TABLE a … <hijack> … CREATE TABLE b …`, run as an unmarked `up`.
"caught" = exit 1, `TxControlInMigration`, nothing recorded in `schema_migrations`.

| # | Attack | Post-body state | Result |
|---|---|---|---|
| 1 | none (control) | INTRANS, same xid | applies, recorded — correct |
| 2 | `COMMIT;` | IDLE | **caught** (`a`, `b` durable, as documented) |
| 3 | `ROLLBACK;` | IDLE | **caught** (`a` gone, `b` durable) |
| 4 | `COMMIT; BEGIN;` | INTRANS forged, xid changed | **caught by the xid check** |
| 5 | `COMMIT AND CHAIN;` (PG14+) | INTRANS **in one statement**, xid changed | **caught** — not in the report's evidence table; the strongest single-statement forgery of INTRANS and the xid check is what stops it |
| 6 | `ROLLBACK AND CHAIN;` | INTRANS, xid changed | **caught** |
| 7 | `END;` (COMMIT synonym) | IDLE | **caught** |
| 8 | `ABORT;` (ROLLBACK synonym) | IDLE | **caught** |
| 9 | `COMMIT; BEGIN; BEGIN; SAVEPOINT s;` | INTRANS, xid changed | **caught** |
| 10 | `PREPARE TRANSACTION 'hj';` | error (`max_prepared_transactions = 0`) | exit 2, nothing recorded, nothing applied |
| 11 | `CREATE PROCEDURE … COMMIT … ; CALL p();` | error — invalid transaction termination | exit 2, nothing applied |
| 12 | `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;` | error — must be first statement | exit 2 |
| 13 | `DISCARD ALL;` | error — cannot run inside a transaction block | exit 2 |
| 14 | search_path shadowing: `CREATE FUNCTION evil.pg_current_xact_id()` + `SET search_path` + `COMMIT; BEGIN;` | INTRANS, xid changed | **caught** — `pg_catalog.` qualification defeats it |
| 15 | `BEGIN;` bare | INTRANS, same xid | tolerated — atomicity intact (verified: both tables in one tx) |
| 16 | `SAVEPOINT s; … RELEASE s;` | INTRANS, same xid | tolerated — atomicity intact |
| 17 | `SAVEPOINT s; … ROLLBACK TO s;` | INTRANS, same xid | tolerated — sub-tx rollback only, `b` correctly absent |
| 18 | PL/pgSQL block with `EXCEPTION WHEN OTHERS` | INTRANS, same xid | tolerated — `pg_current_xact_id()` returns the **top-level** xid, so subtransactions do not false-positive |
| 19 | PL/pgSQL exception handler swallowing `1/0` then doing DDL | INTRANS, same xid | tolerated, applied atomically |
| 20–22 | down-direction hijack (`COMMIT` in a down body); `COMMIT` last statement; `COMMIT` first statement | — | **caught**, `schema_migrations` row correctly *not* deleted |

**I could not break this check.** The status+xid pair is sufficient for every shape I could
construct: ending the transaction is visible in the status, and replacing it is visible in the xid,
which is monotonic and cannot be re-forged short of wraparound.

Two integrity observations (not gate bypasses — see NIT-3):

| Attack | Result |
|---|---|
| Body does `INSERT INTO schema_migrations VALUES ('999','forged',…)` | exit 0; `999` now recorded as applied, so a future real `009` would be silently skipped |
| Body does `DELETE FROM schema_migrations` | exit 0; prior bookkeeping erased in the same transaction that records this migration |

---

## Encoding attacks

Every row refused **at discovery**, before any connection — proven by `schema_migrations` not
existing afterwards.

| Attack | Bytes | Result |
|---|---|---|
| NUL in the up body | `CREATE TABLE users…\x00\nDROP TABLE users;` | exit 1, `NUL byte at offset …`, DB untouched |
| NUL in the down body | `SELECT 1;\x00DROP TABLE users;` | exit 1 — **round-3 NEW2-S2 closed in both directions** |
| NUL at EOF | trailing `\x00` | exit 1 |
| UTF-8 BOM at file start | `\xef\xbb\xbf…` | exit 1, names the BOM |
| **BOM mid-file** | `…;\n﻿SELECT 1;` | exit 2 — server syntax error, loud, nothing applied, nothing recorded |
| UTF-16-LE with BOM | `\xff\xfe…` | exit 1, `not valid UTF-8` |
| UTF-16-BE without BOM | interleaved NULs | exit 1, NUL check fires first |
| Latin-1 high byte | `-- caf\xe9` | exit 1, `not valid UTF-8` |
| Lone surrogate (CESU-8) | `\xed\xa0\x80` | exit 1, `not valid UTF-8` |
| Overlong encoding | `\xc0\xaf` | exit 1, `not valid UTF-8` |
| Truncated multi-byte at EOF | trailing `\xc3` | exit 1, `not valid UTF-8` |
| ZWSP between two statements | `…;​CREATE TABLE after…` | exit 2, whole transaction rolled back — **no partial apply** |

No shape produced a divergence between what libpq executed and what the runner recorded.

---

## Regression-test verification

Five defence reverts on a scratch copy of `db/` (`/tmp/…/scratchpad/revert/db`, never the repo
tree). Baseline on the copy: **76 passed**.

| Revert | FIX_REPORT claim | I measured | Verdict |
|---|---|---|---|
| R1 `up_destructive` forced `False` | 6 red, named | **exactly 6 red, the same 6 names** | **CONFIRMED** |
| R2 sniff disabled (`hit = None`) | 24 red incl. all 6 forgery params | **exactly 24 red**, incl. all 6 `test_forged_contents_…` params | **CONFIRMED** |
| R3 `_assert_tx_intact` → `pass` | 4 red | **exactly 4 red** — stray-COMMIT, stray-ROLLBACK, COMMIT;BEGIN, down-hijack | **CONFIRMED** |
| R3b xid comparison → `same_xid = True` | exactly 1 red | **exactly 1 red** — `test_commit_begin_forging_intrans_is_caught_by_the_xid_check` | **CONFIRMED** — the xid check is independently load-bearing |
| R4 NUL check → `if False:` | 2 red | **exactly 2 red** — `test_nul_byte_rejected[up]`, `[down]` | **CONFIRMED** |
| Perf vs the OLD implementation | old scanner 4.37 s @ 10 kB … timeout | **NOT REPRODUCIBLE** — the round-3 `_scan_sql` was never committed (HEAD holds the round-0 scanner), so the comparison cannot be re-derived from the repo. The *current* path is independently measured linear below. | unverifiable, not disputed |

The revert evidence is real and honest — the counts and names match to the test. **But R2 exposes
the structural problem with the pinned corpus:** disabling the *sniff* turns all six
`test_forged_contents_cannot_apply_a_drop_without_the_flag` params red. That means the corpus is
guarded by the sniff, not by the filename mechanism it claims to demonstrate. Every body in
`FORGERY_BODIES` contains a bare, un-obfuscated `DROP TABLE users;`. The suite therefore contains
**no test that a destructive body the sniff cannot see is stopped** — because nothing stops it.
The file docstring's "prove file CONTENTS cannot influence it" is exactly the claim my §A refutes.

---

## Guarantees preserved after the deletion

~230 lines and one test file were removed. Each surviving guarantee re-verified empirically, not
by reading:

- [x] **Checksums** — editing an applied `001` halts the run at exit 1 *before* pending `002` runs
      (`to_regclass('public.b')` is `NULL`); `status` prints `MISMATCH`.
- [x] **Atomicity of body + bookkeeping** — `CREATE TABLE; INSERT; SELECT 1/0` → exit 2, table
      absent, `schema_migrations` empty.
- [x] **Advisory lock** — two runners started simultaneously against a 3 s migration: both exit 0,
      exactly one bookkeeping row, second runner's wall time 3.04 s (it blocked at connect, then
      found nothing pending). Without the lock the loser would exit 2 on a duplicate key.
- [x] **Lock released on disconnect** — `pg_locks` advisory count 1 while held, 0 after `close()`.
- [x] **Session settings** — `statement_timeout = 0`, `idle_in_transaction_session_timeout = 0`,
      `application_name = rh-migrate`.
- [x] **`--dry-run` still evaluates the gate** — exit 1 for a pending destructive migration, on
      both `up` and `up --target`, and applies nothing when allowed.
- [x] **Rollback always requires the flag** — bare `down`, `down --target 000`, and `down
      --dry-run` all exit 1 without it, marked or unmarked down file.
- [x] **Exit codes** — 0 status/no-op · 1 usage error, validation, sniff refusal, tx hijack ·
      2 SQL failure · 3 bad credentials. All four observed.
- [x] **Orphan detection** — a hand-inserted `009` row surfaces as `ORPHAN` in `status` and warns
      on `up`/`down`.
- [x] **Read-once (TOCTOU)** — swapping the file 0.4 s into a 2 s body changed nothing; the cached
      text executed.
- [x] **Renames did not orphan 001–003** — live `status`: all three `applied`, checksum `ok`, no
      ORPHAN rows, before and after a full teardown/re-apply.
- [ ] **"Any near-miss spelling fails discovery loudly"** — **NOT preserved**, see SHOULD-FIX-2.

### Performance — linear, confirmed

| Input | Discovery time |
|---|---|
| 2 files × ~1 MB (1 realistic, 1 `"SELECT a" + "$b"*500_000` — the old quadratic worst case) | **0.086 s** |
| 600 migrations / 1 200 files | **0.032 s** |
| Single 4 MB line | **0.037 s** |
| 2 MB of `"DROP "` repeated (regex-backtracking bait) then `TABLE t;` | **0.089 s** (refused) |

No superlinear path remains. The `≥1 MB` requirement is met with margin.

### Gates re-run by me

```
.venv/bin/ruff check backend/app src db                 → All checks passed!            (rc 0)
.venv/bin/python -m pytest -q                           → 163 passed in 11.21s
shellcheck -x bin/*.sh                                  → clean                          (rc 0)
bash bin/db_migrate.sh down --allow-destructive --target 000 → 003, 002, 001 rolled back (rc 0)
bash bin/db_migrate.sh up                               → 001, 002, 003 applied          (rc 0)
bash bin/db_migrate.sh status                           → all applied, checksum ok, no orphans
docker inspect rh-db → healthy
```

Constraints honoured: no prune; no `km-*` container/volume/network touched (all 11 healthy
throughout); `data/market/` and `rh_db_data` untouched; every attack migration lived in
`/tmp/claude-1000/…/scratchpad`; probe container `rh-verify-pg` removed after; `git diff` contains
this report and nothing else.

---

## New findings

### BLOCKER

#### **R4-B1 — the "fail-closed" sniff is forgeable into silence; seven bodies applied a real `DROP TABLE`/`TRUNCATE` with exit 0 and no flag**

`DESTRUCTIVE_SNIFF_RE` requires `\s+` between `DROP` and `TABLE`. PostgreSQL's lexer treats a
comment as a token separator, so every one of these is valid SQL the sniff does not see:

```sql
drop/**/table users;          -- 4 characters defeat the backstop
DROP -- x
TABLE users;
DO $$ BEGIN EXECUTE 'DR' || 'OP TABLE users'; END $$;
DO $$ BEGIN EXECUTE 'TRUNC' || 'ATE users'; END $$;
```

All four applied end-to-end: exit 0, no `--allow-destructive`, `users` dropped (or emptied), `002`
recorded as applied. Reproduce: `atk_forgery.py`, rows `comment-between-kw`,
`dashcomment-between-kw`, `blockcomment-multiline`, `dyn-exec-concat`, `dyn-exec-format`,
`chr-built-drop`, `dyn-truncate-concat`.

Why BLOCKER and not SHOULD-FIX: three separate documents assert the opposite property in absolute
terms, and the pinned end-to-end corpus is guarded entirely by this layer (revert R2 proves it).
The gate's real strength — the unforgeable filename — is undiminished, so this is a claim/coverage
defect plus a cheap detection gap, **not** a repeat of rounds 1–3. It does not warrant another
redesign.

Scope honestly: shapes 4–7 (dynamic SQL) cannot be closed by any text analysis and must be
documented as out of reach rather than defended against. Shapes 1–3 can and should be closed.

### SHOULD-FIX

#### **R4-S1 — `DROP OWNED BY` is destructive, plausible in this schema, and not in the sniff list**

`001_core_schema` creates the `rh_app` role. The natural way to retire it —
`DROP OWNED BY rh_app; DROP ROLE rh_app;` — cascades to every object the role owns and applies
unmarked with exit 0 (verified). `DROP MATERIALIZED VIEW` likewise. Unlike the §A shapes this is an
*accident* shape, which is precisely what the sniff exists to catch.

#### **R4-S2 — a near-miss *extension* fails discovery SILENTLY, and `up` reports success**

`002_important.up.SQL` (or a trailing space, or a trailing dot) is dropped by
`path.suffix != ".sql"` before `FILENAME_RE` is ever consulted. Observed:

```
$ migrate up --migrations-dir …
INFO migrate: no pending migrations          ← exit 0
$ migrate status
001  seed  applied  ok                       ← 002 is invisible
```

A deploy reports success while the schema change never ran, and `status` will never mention it.
This directly contradicts ADR-002's "any near-miss spelling fails discovery loudly" — that property
holds only *inside* `.sql`. Suggested: case-fold the suffix test and refuse (loudly) any
non-directory entry whose casefolded name ends in `.sql` variants or matches `\d{3,}_` but not
`FILENAME_RE`.

#### **R4-S3 — the pinned forgery corpus does not test the mechanism it claims to test**

Every `FORGERY_BODIES` entry contains a bare `DROP TABLE users;`, so all six are stopped by the
sniff (revert R2: all six red). Add a case whose destructive statement the sniff *cannot* see
(`drop/**/table`) and assert the behaviour you actually intend; today that test would fail. Amend
`test_runner_db.py:7` and `migrate.py:26-31` to describe the real property: *the filename cannot be
influenced by contents; the sniff is a best-effort accident-catcher with known holes.*

### NIT

1. **`FILENAME_RE` anchors with `$`, which also matches before a trailing newline.**
   `FILENAME_RE.match("002_x.up.sql\n")` returns a match (verified). It is unreachable today only
   because `.suffix` is then `".sql\n"` and the file is skipped first — i.e. the anchor bug is
   masked by the bug in R4-S2. Use `\Z`.
2. **A single bad file blocks every command, including `down` and `status`.** Correct and loud, but
   worth one sentence in the docstring: emergency rollback requires the directory to be clean.
3. **A migration body can write `schema_migrations` directly** — inserting `('999','forged',…)`
   succeeds (exit 0) and would make a future real `009` silently skip; `DELETE FROM
   schema_migrations` also succeeds. Inherent to running bodies as the owner, but the runner could
   assert its own row count post-body, or the docstring could state that bookkeeping integrity
   assumes non-hostile bodies.
4. `TRUNCATE/**/TABLE` is caught only because `TRUNCATE` alone is a keyword — luck, not design.

### PRAISE

- **The core redesign is right and is now demonstrated, not asserted.** Twenty attacks on the
  filename channel — homoglyphs, case, doubling, position, a name containing the word, a symlink
  with divergent link/target names, `..` traversal — produced zero classification forgeries. Moving
  the signal out of the artifact genuinely killed the round-1/2/3 attack class.
- **The status+xid transaction check is the best part of this work.** It survived 22 shapes I
  designed specifically to beat it, including `COMMIT AND CHAIN` (a one-statement INTRANS forgery
  the implementer's own evidence table does not list), `END`, `ABORT`, procedure-level `COMMIT`,
  `PREPARE TRANSACTION`, and a `pg_catalog` shadowing attempt. Schema-qualifying the function was
  not paranoia — it is load-bearing. Tolerating `BEGIN`/`SAVEPOINT` is correct and now proven
  (subtransactions return the top-level xid, so exception handlers do not false-positive).
- **The revert evidence is honest.** Five reverts, five exact matches on both count and test names.
  Nobody padded the numbers.
- **Byte-level rejection is complete**, including the down direction, and every rejection happens
  before a connection is opened.
- **Read-once at discovery** closes TOCTOU properly — the swap experiment had no effect at all.
- **The failure modes are documented as they actually are**, not as they should be: the stray-COMMIT
  test asserts the leaked tables *exist*. That is the right instinct, applied everywhere except the
  sniff's claims.

---

## Recommendation

### **One more small round. Do not redesign.**

The mechanism is sound; the backstop and the prose around it are not. Scope of the fix, in order:

1. Make the sniff comment-tolerant. A separator alternation, no lexer:
   ```python
   _SEP = r"(?:[ \t\r\n\f\v]|--[^\n]*(?:\n|$)|/\*(?:[^*]|\*(?!/))*\*/)+"
   DESTRUCTIVE_SNIFF_RE = re.compile(
       rf"\b(?:DROP{_SEP}(?:TABLE|SCHEMA|DATABASE|OWNED|MATERIALIZED)|TRUNCATE)\b", re.IGNORECASE)
   ```
   Verified by me: catches all three comment shapes and `DROP OWNED BY`, still ignores `DROP INDEX`
   / `DROP CONSTRAINT` / prose like "dropped tables", and produces **no** new hit on the real
   `001`–`003` corpus (up files still clean, down files still marked). 0.089 s on 2 MB of
   backtracking bait.
2. Strike the three false sentences (ADR-002 §Decision 2, FIX_REPORT §Design 2, `migrate.py:30`)
   and replace them with the true one: *the filename is unforgeable; the sniff is best-effort and
   cannot see dynamic SQL.* Add the `EXECUTE`-built-`DROP` shape to the LIMITATION section.
3. Add a regression test with a body the sniff cannot see, asserting whatever you decide the
   intended behaviour is; fix `test_runner_db.py`'s docstring claim.
4. Case-fold / harden the `.sql` suffix filter so a `.SQL` migration cannot be silently skipped
   (R4-S2), and change `$` to `\Z` in `FILENAME_RE` (NIT-1).

Items 1, 2 and 4 are under thirty lines between them. None of them touch the filename grammar, the
transaction check, or the byte-level rejection — those three are finished work and should be left
alone.
