# Verification: B-1 residual fix (dollar-tag grammar, own-line rule, `\r` terminator)

**Verifier:** independent — did not write the runner, the reviews, or either fix-pass ·
**Date:** 2026-07-28 · **Scope:** `db/migrate.py` scanner + directive classification, `db/tests/`,
and the claims in `docs/fixpass/FIX_REPORT.md` § *Follow-up fix-pass — B-1 residual (D1, D2)*.

Method: every forgery below was executed end-to-end through the **actual runner** — `migrate.main()`
with `--migrations-dir` pointing at a scratch directory under `/tmp`, against a **real PostgreSQL 16**
(throwaway container `rh-probe-pg`, created and destroyed by this review; `rh-db` was never a forgery
target). Every grammar claim was re-derived from a live PG16 backend via psycopg's simple protocol,
not via `psql` (whose own lexer is a different program). The repo tree was never used for experiments.

---

## Summary verdict

# **FAIL**

The dollar-quote work (D1) and the `\r` work (D3) are correct, complete, and well-tested — I could not
break either. **D2 is not.** The destructive gate is still forgeable, and I forged it **nine ways**,
seven of them dropping or truncating a real table with **exit 0 and no `--allow-destructive`**.

The round-2 fix moved the own-line check to the raw source, which closes the specific shapes the
re-review produced. But it left two independent holes in the *same* function pair
(`_directive_scan_text` / `DIRECTIVE_RE`), and one of them is strictly worse than the bug it replaced:

> **`DIRECTIVE_RE` is matched against a concatenation of comment text and executable code, and its
> `\s` classes match newlines. Only the literal `--` has to live in a comment. The word `migrate:`
> and the value `non-destructive` can both be executable SQL.**

This is the third consecutive round in which the same guarantee has been asserted and falsified. The
pattern is not bad luck: each round has fixed the *shapes* the previous reviewer produced rather than
the *property* the gate needs. See "Recommendation" for what I think has to change.

Verdict on the individual defects: **D1 FIXED · D3 FIXED · D2 NOT FIXED.**

---

## Forgery attempts

`users` is a table created by migration 001 holding one row. Migration 002 is the attack and contains a
real `DROP TABLE users;` / `TRUNCATE users;`. **FORGED** = `up` *without* `--allow-destructive`
returned exit 0 and the table/rows were gone.

| # | Attack | Shape tried | Result |
|---|---|---|---|
| 1 | **Directive value smuggled from code** | `-- migrate:` on its own line; `non-destructive` is the first token of the next line, an executable SQL expression (`WITH t AS (SELECT 5 AS non, 2 AS destructive) SELECT … non-destructive … FROM t;`) | **FORGED** — exit 0, `users` gone |
| 2 | **Directive keyword *and* value from code** | Comment is bare `--`; `migrate: non-destructive` is an executable array slice `a[migrate : non-destructive]` | **FORGED** — exit 0, `users` gone |
| 3 | Same as 1, value indented | `-- migrate:` then `        non-destructive` | **FORGED** — exit 0, `users` gone |
| 4 | Same as 1, with `TRUNCATE` | `TRUNCATE users;` + split directive | **FORGED** — exit 0, 0 rows |
| 5 | **U+00A0 before the directive** | `CREATE TABLE meta (\n<NBSP>-- migrate: non-destructive\n int);` — NBSP is a legal PG column name; `str.strip()` calls it whitespace | **FORGED** — exit 0, `users` gone |
| 6 | U+3000 IDEOGRAPHIC SPACE, same shape | as 5 | **FORGED** — exit 0, `users` gone |
| 7 | U+2028 LINE SEPARATOR, same shape | as 5 | **FORGED** — exit 0, `users` gone |
| 8 | U+0085 NEL, same shape | as 5 | **FORGED** — exit 0, `users` gone |
| 9 | Flip a benign migration to *destructive* | `-- migrate:` + code line `destructive` | **FORGED** (inverse direction; costs a flag, not data) |
| — | Non-ASCII dollar tags | `$café$ $é$ $€$ $٣$ $日本$ $t1$ $_$ $İ$ $ǅ$ $😀$ $́$ $　$ $﻿$ $​$ $ $` each wrapping a forged directive | BLOCKED (all 17) |
| — | Empty / prefix / case-differing tags | `$$`, `$a$…$ab$…$a$`, `$t$…$T$…$t$` | BLOCKED |
| — | Digit-start tag / positional param | `$1$`, `$1 $$…$$`, `1$$…$$` | BLOCKED (`$1$` is a PG **syntax error** — divergence, but fail-loud; see NIT-2) |
| — | `$` inside an identifier | `a$b$…$b$` | BLOCKED (`SqlLexError`) |
| — | Nested / mismatched tags | `$fn$ … $inner$ … $inner$ … $fn$` | BLOCKED |
| — | Comment terminators | `\r`, `\r\n`, `\n`, `\f`, `\v`, U+2028, U+0085 after a directive | BLOCKED |
| — | Hide a `DROP` after a bare `\r` | `-- note\rDROP TABLE users;` | BLOCKED (D3 works) |
| — | Literals | `E'\\'`, `''` doubling, `U&'…' UESCAPE`, `B'…'`, literal containing `--` / `/*` / `$$` | BLOCKED |
| — | Quoted identifiers | `"col--umn"`, `""` doubling, identifier containing `$$` / newline | BLOCKED |
| — | Block comments | nested, unterminated, directive inside, `/*` inside a line comment, directive trailing `*/` | BLOCKED |
| — | Encoding | UTF-8 BOM at file start, NUL byte, directive with tab/trailing whitespace | BLOCKED (BOM/NUL: see SF-2, NIT-1) |
| — | **Control** | `DROP TABLE users;` + a malformed directive | BLOCKED, exit 1, table + row intact |

Reproduction of #1, byte-for-byte (`002_attack.up.sql`):

```sql
WITH t AS (SELECT 5 AS non, 2 AS destructive)
SELECT
-- migrate:
non-destructive
FROM t;
DROP TABLE users;
```

```
$ python -c "from migrate import main; main(['up','--migrations-dir','…'])"
INFO migrate: applying 002_attack
INFO migrate: applied 002_attack in 4ms      →  exit 0
SELECT to_regclass('public.users')  →  None
```

The file contains **no valid directive**. `-- migrate:` declares nothing; `non-destructive` is an
arithmetic expression that PostgreSQL evaluates to `3`. The runner manufactures a directive by
concatenating the two.

---

## Regression-test verification

I reverted **all five** claimed defences independently on a scratch copy of `db/`
(`/tmp/…/scratchpad/dbcopy`, never the repo tree) and ran the full `db/tests` suite against each.

| Revert | FIX_REPORT claims | I measured | Verdict |
|---|---|---|---|
| A1 `_DOLLAR_TAG_RE` → ASCII-only | 7 unit + 1 E2E | **8 red** — 4 non-ASCII forgery params, unicode-`DO` false positive, round-trip, unterminated, **+ E2E** | **CONFIRMED** |
| A2 identifier-`$` guard removed | 2 | **2 red** — `…dollar_inside_identifier…` + the `a$b$c` round-trip param | **CONFIRMED** |
| A3 E-string lookbehind → ASCII | 1 | **1 red** — `…estring_lookbehind_uses_postgres_identifier_alphabet` | **CONFIRMED** |
| B own-line rule → rebuilt text | 3 unit + 1 E2E | **4 red** — all three `test_news1_…` + **E2E** | **CONFIRMED** |
| C comments end at `\n` only | 2 | **2 red** — both CR tests | **CONFIRMED** |

And they fail for the **right** reason, not incidentally. With A1 reverted, the end-to-end test fails
exactly as the report says:

```
>       assert main(["up", "--migrations-dir", str(tmp_path)]) == EXIT_VALIDATION
E       AssertionError: assert 0 == 1
```

— the gate returned 0, the forged `DROP TABLE` applied. That is a real regression test, not a
tautology. **PRAISE-1 below.**

The revert evidence in the FIX_REPORT is therefore honest and reproducible. Its counts (7/2/1/3/2)
are the *unit* counts with the E2E listed separately in the same cell; my totals (8/2/1/4/2) are the
same numbers added up.

---

## Claim verification

Every claim in the new FIX_REPORT section, checked against the code and against a live PG16.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `$€$`, `$٣$`, `$t1$` are accepted tags on PG16 | **CONFIRMED** | all three return the body as a string |
| 2 | `a$b$c` is ONE identifier | **CONFIRMED** | `WITH t AS (SELECT 1 AS a$b$c) SELECT a$b$c FROM t` → `1` |
| 3 | `SELECT 1$$x$$` lexes as integer-then-string | **CONFIRMED** | `syntax error at or near "$$x$$"` — the parser is handed a string token |
| 4 | `$t$ x $T$ y $t$` is one string (exact, case-sensitive close) | **CONFIRMED** | returns `" x $T$ y "` |
| 5 | `$a$ … $ab$ … $a$` — a prefix tag does not close | **CONFIRMED** | returns `" y $ab$ z "` |
| 6 | `$1 + 1` prepares as a positional parameter | **CONFIRMED** | `PREPARE p1 AS SELECT $1::int + 1; EXECUTE p1(41)` → `42` |
| 7 | Code after a bare `\r` on a `--` line **executes** | **CONFIRMED** | `-- migrate: non-destructive\rSELECT 43` → `43`. D3 is a real defect, not invented complexity |
| 8 | `éE'…'` is identifier `éE` + a PLAIN literal | **CONFIRMED** | PG reports `type "ée" does not exist` — it parsed `ée 'x'` as a typecast |
| 9 | scan.l `dolq_start [A-Za-z\200-\377_]` ≡ code points ≥ U+0080 over UTF-8 | **CONFIRMED** | `$😀$`, `$́$` (combining acute alone), `$﻿$`, `$​$` all accepted as tags; the fixed regex accepts all of them |
| 10 | An ASCII digit cannot start a tag | **CONFIRMED** | `SELECT $1$…$1$` → `syntax error at or near "$"` |
| 11 | The re-review's suggested fix (`\$(?:[^\W\d]\w*)?\$`) would have been insufficient | **CONFIRMED** | `[^\W\d]` excludes `€` and `٣`, both valid tags. The fixer was right to reject the suggestion |
| 12 | D1 "closed for every shape either reviewer produced" | **CONFIRMED** | I re-ran all of them plus 12 new tag forms; none forge |
| 13 | D2: "a directive trailing **ANYTHING** — code, a closing `$$`, a `*/`, a quote — is never honored" | **REFUTED** | 19 Unicode characters that PostgreSQL lexes as *identifier* characters are stripped by `str.strip()`; forgeries #5–#8 |
| 14 | `db/migrate.py:96` "Recognized **ONLY** inside `--` line comments (the scanner guarantees this)" | **REFUTED** | forgeries #1–#4: the value, and even `migrate:`, come from executable code |
| 15 | `db/migrate.py:97-98` "only whitespace may precede it on its line IN THE RAW FILE" | **REFUTED** | same as 13 — `str.strip()`'s whitespace ≠ PostgreSQL's whitespace |
| 16 | "directive-shaped text inside any blanked segment can never forge a classification" | **CONFIRMED** (narrowly) | true for blanked segments — but the forgery now comes from *kept* segments, which the sentence does not cover |
| 17 | `_scan_sql` is "ONE left-to-right pass" (reads as O(n)) | **REFUTED** | the new identifier-`$` backscan is O(n²): 10 kB → 4.6 s, 20 kB → 18.2 s, 40 kB → **72.9 s** (SF-1) |
| 18 | Gates: ruff clean · 179 passed · shellcheck clean | **CONFIRMED** | reproduced verbatim, see below |
| 19 | `rh-db` healthy, 001/002/003 applied / checksum ok, no `km-*` touched | **CONFIRMED** | verified before and after my own live round trip; 11 `km-*` containers healthy throughout |

### Gates re-run by me

```
$ .venv/bin/ruff check backend/app src db          → All checks passed!
$ .venv/bin/python -m pytest -q                    → 179 passed in 7.70s
$ shellcheck -x bin/*.sh                           → rc=0, no output
$ bash bin/db_migrate.sh down --allow-destructive --target 000
                                                   → 003, 002, 001 rolled back
$ bash bin/db_migrate.sh up                        → 001, 002, 003 applied
$ bash bin/db_migrate.sh status                    → 001/002/003 applied, checksum ok
$ docker inspect --format '{{.State.Health.Status}}' rh-db   → healthy
```

---

## New findings

### BLOCKER

#### **NEW2-B1 — the destructive gate is still forgeable: `DIRECTIVE_RE` matches across the comment/code boundary**

`db/migrate.py:100` (`DIRECTIVE_RE`) · `db/migrate.py:336-357` (`_directive_scan_text`)

`_directive_scan_text` returns code segments **and** own-line comment segments concatenated, and
`DIRECTIVE_RE` is then run over that string with `re.MULTILINE`. Python's `\s` matches `\n`. So the
pattern

```python
r"^\s*--\s*migrate:\s*(destructive|non-destructive)\s*$"
```

is free to start inside a `--` comment and finish inside **executable SQL on a later line**. The only
component that must actually be inside a comment is the two-character `--`:

* `--\s*` may cross the newline → `migrate:` can be code (`a[migrate : non-destructive]`, a legal
  array slice — forgery #2).
* `migrate:\s*` may cross the newline → the value can be code (forgery #1, #3, #4).
* `\s*$` matches at the end of the value's line, so the code line only has to *end* after the value.

Four end-to-end drops and one truncation, all exit 0, all without `--allow-destructive`. This is
strictly a superset of the original B-1: B-1 required the forged text to sit inside a token the
scanner mis-lexed; this requires no mis-lexing at all — the scanner classifies every segment
**correctly** and the gate is forged anyway, because the *regex* is applied to a projection where
segment boundaries have been erased.

The round-2 fix is what makes this reachable in its sharpest form: `_directive_scan_text` now blanks
non-own-line comments but still emits code verbatim, so code text is adjacent to comment text with
nothing marking the seam.

**Direction of a fix (not applied — I do not fix):** stop regex-ing the reassembled text. Match the
directive **per line-comment segment**, against that segment's own text only, with the own-line check
already in place. Something shaped like:

```python
for kind, text in _scan_sql(sql):
    if kind is _LINE_COMMENT and _own_line(sql, pos):
        m = DIRECTIVE_RE.fullmatch(text.strip())   # text is ONE comment, no newlines in it
```

with `DIRECTIVE_RE` reduced to `--\s*migrate:\s*(destructive|non-destructive)` and `\s` narrowed
(see NEW2-S1). That makes "a directive is a property of a single comment token" structural rather
than a regex accident, and it kills NEW2-S1 in the same move.

---

### SHOULD-FIX

#### **NEW2-S1 — `str.strip()` / `\s` use Unicode whitespace; PostgreSQL's whitespace is `[ \t\n\r\f]` and everything else is an identifier character**

`db/migrate.py:327-333` (`_own_line`) · `db/migrate.py:100` (`DIRECTIVE_RE`, `^\s*`)

`_own_line` decides "only whitespace precedes this on its raw line" with Python's `str.strip()`.
`str.strip()` strips 24 characters that PostgreSQL does **not** treat as whitespace. I verified
against a live PG16 that `\v` (U+000B) is **not** whitespace to the backend (`SELECT\x0b1` is a syntax
error) while `\f` **is**, and that every non-ASCII one of them is an `ident_start` character — the
same `[A-Za-z\200-\377_]` byte class the D1 fix correctly translated for dollar tags, applied
inconsistently here.

Full set that defeats the own-line rule:

* **19 that PostgreSQL lexes as a legal identifier** (exploitable): U+0085, U+00A0, U+1680,
  U+2000–U+200A, U+2028, U+2029, U+202F, U+205F, U+3000.
* **5 that PostgreSQL rejects outright** (fail-loud, harmless): U+000B, U+001C–U+001F.

Demonstrated end-to-end (forgeries #5–#8): `CREATE TABLE meta (\n<U+00A0>-- migrate: non-destructive\n
int);` creates a column literally named U+00A0, so the file is valid SQL, real code precedes the
directive on its raw line, and the gate honors it anyway. `DROP TABLE users` then applied at exit 0.

I rate this SHOULD-FIX rather than BLOCKER only because the honored text is a *visible*
directive — the smuggled character is invisible, so a reviewer reading the diff sees a directive that
is in fact active. It is nonetheless the documented guarantee at `db/migrate.py:97-98`, verbatim,
being false, and it is the same class of "our character classes are not Postgres's character
classes" defect that D1 was raised for.

**Fix:** use PostgreSQL's whitespace set explicitly, e.g. `sql[line_start:pos].strip(" \t\r\n\f")`
(or a `_PG_SPACE = " \t\n\r\f"` constant used in both places), and replace the `\s` classes in
`DIRECTIVE_RE` with `[ \t]` / that constant. Pin U+00A0 and U+3000 as regression cases.

#### **NEW2-S2 — a NUL byte silently truncates what PostgreSQL executes; the runner still records the migration as fully applied**

`db/migrate.py:445-446` (`read_text`) · `:562` (`cur.execute(mig.up_sql)`)

libpq transports the query as a C string. A `\x00` in a migration file truncates it at the driver
boundary, but `discover_migrations` reads, validates, classifies and **checksums the whole file**.
Demonstrated:

```
002_attack.up.sql:  CREATE TABLE before_nul (i int);\n\x00\nCREATE TABLE after_nul (i int);\n
$ up  → exit 0, 002 recorded applied
to_regclass('before_nul') = 'before_nul'      to_regclass('after_nul') = NULL
```

Half the migration ran; the runner reports success and the checksum matches forever after. This
directly contradicts the module docstring's *"the SQL that is validated, classified, and checksummed
is byte-for-byte the SQL that executes"* (`db/migrate.py:11-12`). The gate consequence is only
fail-safe (text after a NUL is analysed but not executed → false positives, never false negatives),
but the atomicity consequence is not: a partially-applied migration is supposed to be impossible.

**Fix:** one line in `discover_migrations` — reject any body containing `\x00`.

#### **NEW2-S3 — the round-2 identifier-`$` guard is O(n²) and is by far the worst pathological input in the module**

`db/migrate.py:276-280`

The backscan added by D1's second sub-fix walks left over `ident_cont` characters on **every** `$`,
bounded only by `code_start`. On a long identifier run it is quadratic:

| input | length | `_scan_sql` |
|---|---|---|
| `"SELECT a" + "$b"*5000` | 10 kB | **4.55 s** |
| `"SELECT a" + "$b"*10000` | 20 kB | **18.23 s** |
| `"SELECT a" + "$b"*20000` | 40 kB | **72.86 s** |

Clean 4× per doubling. For comparison the previously-reported NEW-N2 (nested block comments) needs
120 kB to reach 0.5 s — this is roughly **three orders of magnitude worse per byte**, and it is new in
round 2. A 100 kB file of this shape wedges the runner for ~8 minutes with no output.

Not a live risk (migration files are repo-authored and bind-mounted `:ro`), but it falsifies the
"ONE left-to-right pass" framing at `db/migrate.py:22-23` more sharply than NEW-N2 did, and the fix is
trivial: carry the identifier-run state forward in a variable instead of rescanning
(track `prev_is_ident_cont` / `run_started_with_ident_start` as you advance `i`), making the guard O(1)
per character.

For completeness: everything else is well-behaved. 10 MB of realistic migration text scans in 15 s;
`many $$ in comments`, `many dollar quotes` and unterminated tokens are all linear or better; nothing
hangs, and there is no regex backtracking anywhere (the scanner is a hand-written loop and
`_DOLLAR_TAG_RE` is anchored with `.match`).

---

### NIT

* **NEW2-N1 — a UTF-8 BOM makes a legitimate directive silently inert.** `﻿-- migrate:
  non-destructive\nDROP TABLE scratch;` → `str.strip()` does *not* strip U+FEFF, so `_own_line` is
  False and the directive is dropped; classification falls to the sniff → blocked. Fail-safe, and a
  BOM breaks the SQL anyway (PG: `syntax error at or near "﻿SELECT"`), so the only cost is a
  confusing error. Worth rejecting a BOM explicitly at discovery alongside NEW2-S2's NUL check.
* **NEW2-N2 — `$1$` diverges from Postgres.** `SELECT $1$\n-- migrate: non-destructive\n$1$;` is a
  syntax error to PG16 but the runner scans the interior as code and honors the directive. Fail-loud
  (the migration cannot execute, so no data is lost) — but it is a classification divergence in the
  gate's core, and one more reason to make the directive rule structural rather than textual.
* **NEW2-N3 — invalid UTF-8 in a migration file is an uncaught `UnicodeDecodeError`.**
  `read_text(encoding="utf-8")` at `db/migrate.py:445` is not inside the `except MigrationError`
  funnel in `main`, so a mis-encoded file produces a traceback instead of the module's otherwise
  uniformly clean one-line diagnostics. Fail-safe, cosmetic.
* **NEW2-N4 — the sniff is evadable by string concatenation.** `DO $$ BEGIN EXECUTE 'DR' || 'OP TABLE
  users'; END $$;` carries no directive and does not trip `DESTRUCTIVE_RE`. This is inherent to a
  sniff and the docstring already says so (`db/migrate.py:89-92`) — recording it only so the next
  reviewer does not raise it as new.

---

### PRAISE

* **NEW2-P1 — the D1 dollar-tag work is genuinely correct, and correct for the *right* reason.**
  The fixer was handed a suggested patch by the re-review, tested it against a live PG16, found it
  **wrong** (`[^\W\d]` excludes `€` and `٣`, both valid tags), and went to `scan.l` instead. That is
  the difference between fixing a finding and fixing a defect. I threw 17 tag forms at it — combining
  marks alone, emoji, ZWSP, BOM-as-tag, title-case `ǅ`, `İ`, CJK — and every one is handled exactly as
  PostgreSQL handles it. The two "same-mechanism gaps closed alongside" (`a$b$c`, the `éE'…'`
  lookbehind) were not asked for and are real; I confirmed both against the server.
* **NEW2-P2 — D3 was found by the fixer, not by a reviewer, and it is a real fail-open.** `-- note\r
  DROP TABLE t;` executed the DROP on the live server (I reproduced it: `43` came back). Finding a
  third instance of the bug family while fixing the second is the behaviour you want from a fix-pass.
  The chosen resolution — terminate at `\r`, but deliberately do **not** honor a directive on a CR-only
  line — picks the fail-safe side of an ambiguity and says so in a comment.
* **NEW2-P3 — the regression corpus is load-bearing.** All five reverts reproduced, the counts match,
  and the two end-to-end tests fail with `assert 0 == 1` and a vanished table rather than an incidental
  error. Second reviewer in a row to verify this and it held up both times.
* **NEW2-P4 — the FIX_REPORT does not oversell within its stated scope.** Its claim is "closed for
  every shape either reviewer produced", and that is *exactly* true — I re-ran all of them. Its revert
  table is accurate. The two refuted claims (#13, #14 above) are in the source comments, and both were
  inherited-and-restated rather than newly invented. The honesty problem here is the *scope* of the
  guarantee, not the reporting of the work.

---

## Recommendation

### **Another fix-pass is needed.** Do not ship.

One BLOCKER (NEW2-B1) with four working end-to-end forgeries of the exact gate B-1 was raised for, plus
three SHOULD-FIX items, one of which (NEW2-S1) is a fifth through eighth forgery of the same gate.

Scope for the next pass — small, and all in one place:

1. **NEW2-B1** — match the directive against a single line-comment segment's own text
   (`fullmatch` on `text.strip(PG_SPACE)`), never against reassembled text. This is a ~5-line change to
   `explicit_destructiveness` / `_directive_scan_text` and it subsumes NEW2-S1 and NEW2-N2.
2. **NEW2-S1** — introduce `_PG_SPACE = " \t\n\r\f"` and use it in `_own_line` and in the directive
   pattern. No `\s`, no `str.strip()` with no argument, anywhere in the directive path.
3. **NEW2-S2** — reject `\x00` (and, per NEW2-N1, a leading U+FEFF) in `discover_migrations`.
4. **NEW2-S3** — make the identifier-`$` guard O(1) per character.

Regression tests the next pass must add, at minimum: the split-directive shapes (value-from-code,
`migrate:`-from-code, indented value), U+00A0 and U+3000 before a directive, a NUL-bearing body, and an
end-to-end `assert EXIT_VALIDATION` for at least the first and second of those.

### A note on process, since this is round three

Rounds 1, 2 and 3 each ended with the same guarantee restated and each was falsified by the next
reviewer, using a shape the previous round had not enumerated. The reason is visible in the code: the
guarantee is *"a directive is only honored when it is a standalone `--` comment"*, but nothing in the
implementation **represents** that. The scan produces typed segments — correct, and it stays correct
under every input I could construct — and then the directive check throws that typing away by
reassembling everything into one string and running a regex over it. Every round has patched the
reassembly (add a keep-set; blank harder; check the raw line) rather than deleting it.

Until the directive is read **out of a single segment the scanner already identified**, the next
reviewer will find a fourth shape. I would treat "no regex is ever run across a segment boundary" as
the acceptance criterion for round four, not "the shapes in REVIEW_FIXES_b1_residual.md are blocked".

---

## Environment / constraints

* No `docker system prune` or bulk prune was run. No `km-*` container, volume or network was touched;
  all 11 were healthy before and after.
* `data/market/` untouched; the `rh_db_data` volume was never removed.
* All attack migrations lived in `/tmp/…/scratchpad/`. `db/migrations/` is unchanged (6 files, as
  before). The scratch copy of `db/` used for the reverts lived in `/tmp` and the repo tree was never
  reverted.
* One throwaway `postgres:16-alpine` container (`rh-probe-pg`, host port 127.0.0.1:15499) was created
  for the lexer probes and the forgery runs, and **removed** at the end of this review. All forgeries
  ran against it, never against `rh-db`.
* The instructed live round trip (`down --allow-destructive --target 000` → `up`) was run against
  `rh-db`; every user table was empty beforehand (only `schema_migrations` held rows). `rh-db` is left
  running, healthy, with 001-003 applied and checksums `ok`.
* `git diff` contains none of my experiments. (`PROJECT_PLAN.md` and `docs/DATA_INVENTORY.md` show as
  modified with mtimes of 13:08/13:12 today, ~6 h before this review began, and contain unrelated
  project-plan prose — not mine.)
