# Review: Phase A loaders

**Reviewer:** independent senior review (Python correctness + operational robustness scope)
**Date:** 2026-07-29
**Scope:** `db/load_minute_bars.py`, `db/load_daily_bars.py`, `db/load_corporate_actions.py`,
`db/load_reference_data.py` — Python and operational behavior only. SQL migrations and
financial/domain semantics belong to other reviewers; where a finding straddles the boundary it is
flagged for coordination rather than adjudicated here.

**Method:** full read of all four loaders against `SENIOR_ENGINEER_BAR.md` (§0, §1, §7.2),
`docs/DATA_INVENTORY.md`, ADR-001/ADR-002; constraint verification against migrations 001–006;
live verification against `rh-db` (migrations 001–006 applied, checksums ok; 12,840,439 daily
bars, 19,301 securities, 46,934 actions, 2,557 calendar days, 18,133 rates) and against the real
archive including all 15 corrupt members. Every load-bearing claim below was executed, not
assumed. No data was modified: all loader runs used `--dry-run`, all SQL was read-only.

---

## Summary verdict: REQUEST CHANGES

Two blockers. **B-1:** `load_corporate_actions.fetch_actions` swallows every provider failure at
DEBUG level and the run exits 0 — a rate-limited or egress-broken fetch is indistinguishable from
"this symbol has no corporate actions," after which `adjust` stamps `split_adj_factor = 1` onto
securities whose splits were never fetched. That is silent, persistent corruption of the return
series, exactly the failure class §0 [P0] fail-loud exists to prevent. **B-2:** `load_minute_bars`
has none of the corrupt-archive handling its sibling was given, and the archive it exists to load
contains 15 corrupt members — verified live: it dies on `2024-12-10.csv.gz` with a raw
`zlib.error` traceback, and a full-archive run can never complete without hand-quarantining files.

The resumability core, by contrast, is genuinely sound — I probed it hard and found no hole (see
Praise). Both bar loaders are single-transaction-per-file with the provenance row inside the
transaction, backstopped by `uq_data_sources_artifact` and the bar-table PKs. There is no path I
could construct where a file is recorded loaded with its rows absent, or its rows loaded twice.

---

## Bar checklist

| Bar rule | minute_bars | daily_bars | corp_actions | reference_data |
|---|---|---|---|---|
| §0 [P0] Robust by default (I/O failure handled) | **FAIL** (B-2: corrupt member → raw traceback) | PASS (verified live, 15/15 skipped) | **FAIL** (B-1: provider errors swallowed) | PARTIAL (S-3: no retry on FRED fetch) |
| §0 [P0] Resume/idempotency where partial run could double | PASS (verified) | PASS (verified) | PASS (`ON CONFLICT`, with S-5 caveat) | PASS (upsert / `DO NOTHING`) |
| §1.8 [P0] No bare `except Exception` swallow | PASS | PASS | **FAIL** (line 153, B-1) | PASS |
| §1.8 [P0] Timeouts + retries w/ backoff on outbound I/O | n/a (DB only) | n/a | **FAIL** (no timeout/retry control over yfinance) | PARTIAL (timeout yes, retry no — S-3) |
| §7.2 [P0] Never float for money | PASS (prices COPY'd as source strings) | **PARTIAL** (S-2: high/low via float) | PARTIAL (ratios/amounts via `float()`, see S-2 note) | PARTIAL (rate via float; absorbed by NUMERIC(9,6) scale — N-1) |
| §1.2 [P0] Full type hints, no wrong annotations | FAIL (N-2: `_open_csv -> csv.reader` is wrong) | PARTIAL | PARTIAL | PARTIAL |
| §0 [P1] Fail loud — skipped data always reported | PASS | PASS | **FAIL** (B-1: DEBUG-level, uncounted) | PASS |
| §3.12 [P0] No secrets/DSN in logs | PASS (all four: DSN never logged, password via env not argv) | PASS | PASS | PASS |
| Exit-code discipline (own contract: 0/1/2/3) | PARTIAL (corrupt file → uncaught traceback, exit 1 by accident) | PASS (verified: exit 1 with corrupt list) | **FAIL** (provider failure → exit 0) | PARTIAL (network failure → 1, should be 3) |
| §5.2 tests for the logic | **absent** | **absent** | **absent** | **absent** (S-7) |

---

## Findings

### BLOCKER

- **B-1** — `load_corporate_actions.py:135-155` + `213-215`: provider failures are silently
  converted to "no actions"; run exits 0; `adjust` then bakes the omission into
  `split_adj_factor = 1`. Silent data loss.
- **B-2** — `load_minute_bars.py`: no corrupt-archive handling at all; crashes with an uncaught
  `zlib.error`/`BadGzipFile` traceback on the first of 15 known-corrupt members of the only
  archive it exists to load (verified live). A full minute load structurally cannot complete.

### SHOULD-FIX

- **S-1** — `load_daily_bars.py:192,208,215`: corrupt-member catch tuple misses
  `UnicodeDecodeError` and `csv.Error`, both reachable from a corrupt-but-inflatable stream;
  either escapes as a raw traceback and aborts the run.
- **S-2** — `load_daily_bars.py:116-117,248,345`: `DayBar.high`/`low` pass through Python
  `float` on their way to a NUMERIC column while `open`/`close` on the same row stay strings.
  §7.2 [P0] violation; loss envelope quantified as nil for this data, fix is trivial.
- **S-3** — `load_reference_data.py:245-249`: FRED fetch has no retry/backoff (§1.8 [P0] for an
  idempotent GET) and a network failure exits `EXIT_VALIDATION` (1) instead of
  `EXIT_CONNECTION` (3).
- **S-4** — `load_reference_data.py:279-294`: `data_sources` provenance row committed before and
  outside the rates transaction — an interrupt leaves a row claiming `row_count=18k` with zero
  rows landed. Both bar loaders put it inside the transaction; this one should too.
- **S-5** — `load_corporate_actions.py:221-233`: `ON CONFLICT DO NOTHING` silently discards a
  conflicting action whose values differ (provider revision; second same-type action on one
  ex_date). Not even counted. Detect `rowcount == 0` + value mismatch and warn.
- **S-6** — `load_daily_bars.py:82` (`SESSION_LAST_MINUTE`): fixed 09:30–15:59 window ignores
  13:00 ET early closes. Empirically benign in THIS archive (verified: the 13:00 auction bar is
  captured, near-zero bars print after) but inconsistent with regular days, where the 16:00
  auction bar is EXCLUDED (SPY 2024-11-27: stored close 598.79 = 15:59 bar; 16:00 bar closes
  598.82). Needs a domain ruling — coordination item, evidence below.
- **S-7** — no tests exist for any loader (`db/tests/` covers the migration runner only). The
  pure functions (`nyse_holidays`, `session_bounds_ns`, `DayBar.update`, FRED CSV parse,
  `SYMBOL_RE`) are trivially testable and guard a multi-hour irreplaceable load.

### NIT

- **N-1** — `load_reference_data.py:266,271`: rate goes through `float(raw)/100.0`, producing
  artifacts like `0.015300000000000001` (verified); `NUMERIC(9,6)` scale rounding absorbs them
  exactly, so no stored corruption — but `Decimal(raw)/100` costs nothing and removes the
  dependence on column scale for correctness.
- **N-2** — `load_minute_bars.py:131`: `_open_csv(path: Path) -> csv.reader` — annotation is
  wrong twice (returns a `(fh, reader)` tuple; `csv.reader` is not a type).
- **N-3** — both bar loaders: `if args.limit:` treats `--limit 0` as "no limit"; enforce `>= 1`
  in argparse.
- **N-4** — all loaders: a mid-run dropped connection surfaces as `psycopg.Error → EXIT_SQL`
  (2); `psycopg.OperationalError` should map to `EXIT_CONNECTION` (3).
- **N-5** — `load_corporate_actions.py:137`: `import yfinance` sits inside `fetch_actions` but
  outside its `try`; a missing module is a raw traceback, unlike the guarded psycopg import at
  module top.
- **N-6** — `load_corporate_actions.py:267-271`: comment says NUMERIC(30,10) "tops out below"
  ~1e13; it holds up to 1e20−ε. The `< 1e19` guard is correct; the comment is misleading.
- **N-7** — `load_daily_bars.py`: `skipped` mixes units (skipped minute rows in
  `aggregate_file`, skipped day-bars in `load_file:337,343`) into one reported number.
- **N-8** — `load_daily_bars.py:120-131`: two rows with identical `window_start` for one symbol
  would double-count volume silently (neither `<` nor `>` branch fires, `volume += v` always
  runs). Zero duplicates found in a sampled 1.6M-row file, and the minute loader would instead
  abort loudly via PK — inconsistent failure modes for the same defect.
- **N-9** — `load_corporate_actions.py:115-131`: `candidates_all` does not filter
  `delisted_at IS NULL` while `candidates_named` does — inconsistent (all is arguably the
  correct one, since delisted securities still have bars needing adjustment).

### PRAISE

- **P-1** — Resumability design is genuinely correct, not just claimed (detail in §Detailed).
- **P-2** — Minute-bar prices reach COPY as untouched source strings (`load_minute_bars.py:223,
  259`): the money path has zero float in it. This is the §7.2 discipline done right, and it is
  the standard S-2 measures against.
- **P-3** — `synchronous_commit = off` is reasoned, scoped, and carries an explicit do-not-copy
  warning for order-state writers (`load_minute_bars.py:342-347`, `load_daily_bars.py:361-364`).
- **P-4** — The daily loader's corrupt-archive machinery works exactly as documented — verified
  live against all 15 corrupt members: skip, report, exit 1, and the `except CorruptArchive`
  before `except LoadError` ordering (subclass first) is correct.
- **P-5** — NYSE calendar rules verified correct for every year 2020–2026, including the New
  Year's Saturday non-observance, Juneteenth starting 2022, the Carter closure, and the
  conditional July-3/Christmas-Eve early closes.
- **P-6** — `warn_if_archive_incomplete` (`load_corporate_actions.py:158-179`) is an honest
  operational guard encoding a real incident instead of pretending to validate.
- **P-7** — `bin/db_load_bars.sh`: password via environment (never argv), repo mounted
  read-only, `--init` for signal delivery on a 20-hour run, host-uid to respect the 0700 data
  directory. Careful work.

---

## Detailed findings

### B-1 — provider failures silently become "no actions", and the run reports success

`load_corporate_actions.py:135-155`:

```python
except Exception as exc:  # noqa: BLE001 — one bad symbol must not end a 3,700-symbol run
    logger.debug("%s: provider error: %s", symbol, exc)
return splits, divs
```

The stated intent — one bad symbol must not end the run — is right. The implementation fails it
three separate ways:

1. **DEBUG level.** At the default INFO level the message is invisible. A run where yfinance
   rate-limits the last 2,000 of 3,700 symbols prints plausible-looking counts and nothing else.
2. **Not counted.** The `errors` counter (`cmd_fetch:210,236`) counts only *insert* errors.
   Provider errors increment nothing, so the summary line ("%d insert errors") actively
   misleads.
3. **Exit 0.** `cmd_fetch` returns `EXIT_OK` (line 252) regardless. Run this loader through
   `bin/db_load_bars.sh` (DB-only network, no egress) instead of `bin/db_corporate_actions.sh`
   and every single call fails: the run logs "0 securities had actions", exits 0, and nothing
   distinguishes that from a universe with no splits.

The damage is not contained to the fetch. `cmd_adjust` (lines 309-329) then writes
`split_adj_factor = 1` for every security without recorded actions — including the ones whose
fetch silently failed — so the omission is baked into `price_bars_daily` as a positive claim
("no split"). `verify` partially mitigates: it catches residual gaps near round ratios for
securities priced ≥ $1 whose move persisted — but small ratios, dividends, and sub-$1 names are
invisible to it, and `verify` is a separate command an operator must think to run.

This meets the blocker bar directly: silent data loss with a success exit code. §0 [P0]
("no silent catch-and-ignore"), §1.8 [P0] ("no bare except… log with context, re-raise or handle
deliberately"), §3.2 A10 (fail closed, no silent catch).

**Minimum fix:** catch narrowly what yfinance actually raises where identifiable; count provider
failures per symbol; log at WARNING with the symbol; report the count in the summary; return
non-zero (or at minimum a loud refusal to proceed to normal exit) when the failure rate is
non-trivial — e.g. any failures → `EXIT_VALIDATION` with the symbol list, mirroring how
`load_daily_bars` treats corrupt files. A total-failure short-circuit (first N symbols all fail →
abort as connectivity error) would also catch the wrong-wrapper case in seconds instead of hours.

### B-2 — the minute loader cannot survive the archive it was built for

`load_minute_bars.py` imports `gzip` but not `zlib`, defines no `CorruptArchive`, and its `main`
catches only `psycopg.Error` and `LoadError` (lines 395-403). Decompression failures surface
mid-iteration in `scan_file:156` (first pass) or `_rows:216` (second pass, inside the COPY).

Verified against the actual archive (host Python, then live in the loader's own container):

- All 15 corrupt members classified: `2024-12-10.csv.gz` raises `zlib.error` after 1,266,148
  rows; the other 14 raise `gzip.BadGzipFile` ("Not a gzipped file") at first read.
- Live run: `bash bin/db_load_bars.sh --dry-run --root /repo/data/market/minute_bars_5y/2024/12`
  → uncaught `zlib.error: Error -3 while decompressing data: invalid block type` from
  `scan_file` (traceback through `load_minute_bars.py:394 → 280 → 156`), interpreter exit 1.

Consequences of a real full-archive run: the loader processes ~1,049 files (~20 hours at the
docstring's measured rate), then dies with a raw traceback at `2024-12-10.csv.gz`. No data is
lost — the per-file transaction and hash-resume hold (a mid-COPY failure rolls back cleanly) —
but every resume re-hashes and crashes at the same file, and the ~188 files of 2025 are
unreachable until the operator hand-removes or re-copies 15 files. The exit code is 1 only by
interpreter accident, violating the wrapper's own documented contract
(`bin/db_load_bars.sh:20`: "Exit codes are the loader's").

The sibling loader solved exactly this problem against exactly this archive and documents the 15
corrupt members in its docstring (`load_daily_bars.py:94-101`). Shipping the minute loader
without the same machinery is not a defensible difference in requirements; it is an
inconsistency. **Fix:** port `CorruptArchive` + `_rows_or_corrupt` + the header/open guards from
`load_daily_bars.py:181-227` (with the S-1 widening below), and the corrupt-collection loop +
final `EXIT_VALIDATION` report from `main` (lines 404-447). Because the failure can occur during
the second pass (inside the file's transaction), confirm the rollback path stays clean — it does
today; keep it that way by raising out of the `with conn.transaction()` block.

### S-1 — the corrupt-member catch tuple is incomplete

`load_daily_bars.py:192` (and the same tuple at 208 and 215) catches
`(OSError, EOFError, zlib.error)`. Two reachable escapes:

- **`UnicodeDecodeError`** — a `ValueError` subclass, not `OSError`. `gzip.open(path, "rt")`
  decodes decompressed chunks as UTF-8 *before* the member CRC is checked; a corruption pattern
  that still inflates (valid Huffman stream, garbage output) raises it from `next(reader)`.
- **`csv.Error`** — garbage that decodes (e.g. long NUL/zero runs decode fine as UTF-8) can
  produce a field exceeding `csv.field_size_limit()` (131,072), raising
  `_csv.Error: field larger than field limit` from `next(reader)`.

Neither is caught in `_rows_or_corrupt`, in `aggregate_file`, or in `main` — either one is a raw
traceback that aborts the whole run, defeating the module's own stated design goal
("crashing on the first would have hidden the other 14", line 99-100). All 15 *current* corrupt
members happen to raise covered types (verified above), so this is latent, not active — but the
whole point of this handler is corruption patterns you haven't seen yet.

On the flip-side question — does catching bare `OSError` in the row loop swallow a real I/O
failure that should halt? Partially, by design: a genuine `EIO` from a failing source disk is
reported as "corrupt gzip stream", the file is skipped, and the run continues to a non-zero exit
with the file named. The information is preserved and loud, so I do not rate it a defect; but
note that a *systemic* disk failure will produce up to 1,256 "CORRUPT" reports rather than an
early halt, and `sha256_of` (line 134-139) has no handling at all — an `OSError` there (file
vanished, permission) is an uncaught traceback. Worth folding into the same fix.

### S-2 — `DayBar.high`/`low` take the float path to a NUMERIC column

`load_daily_bars.py:248` converts (`h, low_v = float(high), float(low)`), the `DayBar` stores
them as `float` (lines 116-117), and `cp.write_row(...)` (line 345) sends the floats — while
`open`/`close` on the *same row* are carried as source strings end-to-end. §7.2 [P0] is
unambiguous: never float for money.

I quantified the actual loss envelope rather than hand-waving: psycopg emits the float's
shortest-round-trip repr, so the stored NUMERIC differs from the source decimal only when the
source string is not the canonical repr of its double — i.e. > ~15-17 significant digits — and
the column's NUMERIC(18,6) scale then rounds to 6 dp, which absorbs the double's ~1e-16 relative
error for any price below ~1e9. For this dataset the path is demonstrably lossless. It is still
wrong to keep: the guarantee now depends on column scale and price magnitude instead of on the
code, the same file proves the string path costs nothing (that is how `open`/`close` already
travel), and comparisons for high/low tracking work identically on `Decimal` or on
string-compare-after-float (keep floats for the *comparisons* if the CPU matters — but write the
*source strings*). Fix: store the string alongside, or store `Decimal`; either preserves the
"max/min by comparison" logic (`DayBar.update:127-130`) unchanged in behavior.

Related, both loaders' float *pre-screens* (`load_minute_bars.py:240-242`,
`load_daily_bars.py:341-344`) guard an exact-NUMERIC CHECK with an approximate float comparison:
a row where `open` exceeds `high` by less than one double ulp passes the screen and aborts the
whole file's COPY at the DB CHECK. Purely theoretical for real price data; noting it because the
screen's stated purpose is "keep one corrupt row from aborting a 1.4M-row COPY" and the guard is
weaker than the constraint it fronts.

### S-3 / exit codes — reference-data fetch

`load_reference_data.py:245-249`: single attempt, no backoff, against a rate-limited public
endpoint — §1.8 [P0] requires retry-with-backoff for idempotent transient I/O, and this GET is
the definition of one. The failure is then raised as `LoadError`, which `main:387-389` maps to
`EXIT_VALIDATION` (1); a network failure reporting itself as a validation failure breaks the exit
contract (connection problems are 3). The `timeout=60` is present and correct. SSRF/injection:
none — `FRED_URL` is a constant built from a constant (`load_reference_data.py:73-74`), no user
input reaches it; scheme is fixed `https`.

`known_at` (probe 8): applied in exactly one place (`cmd_rates:273`) as
`effective_date + 1 day at 00:00 UTC`. The CHECK (`004_evaluation.up.sql:85`) anchors
`effective_date` at UTC midnight, so `known_at` exceeds it by exactly 24h for every row —
consistent, and cannot violate the constraint. The DTB3 rate range (±16% historically) sits well
inside the ±1 CHECK given the /100 scaling.

Holiday rules (probe 8): walked every year 2020–2026 against NYSE reality: 2020 July-3
observance ✓, no COVID closure (correct — NYSE stayed open electronically) ✓; 2021 Dec-24
Christmas observance ✓, no Juneteenth (correct, NYSE began 2022) ✓; 2021-12-31 trades despite
NYD-2022 Saturday (the `_observed_new_year` special case, lines 126-142) ✓; 2022 Juneteenth
Monday observance ✓; 2023 July-3 early close ✓; 2024 Dec-24 early close ✓; 2025 Carter closure
Jan-9 ✓ and July-3 early close ✓; 2026 July-3 full holiday (Sat observance) with correctly *no*
July-2 early close (matches 2020 precedent) ✓. No errors found. `cmd_calendar`'s upsert
(lines 224-235) is idempotent and covers the full range including closed days — 2,557 rows live,
matching the default range exactly.

### S-4 — provenance row outside the data transaction

`load_reference_data.py:279-287` commits the `data_sources` row (autocommit, claiming
`row_count = len(rows)`) *before* opening the transaction that inserts the rates (289-294). An
interrupt between the two leaves provenance asserting ~18k rows that never landed, and the next
run — having no hash to dedupe on — adds a second `data_sources` row (rows dedupe via
`ON CONFLICT`, so no data harm, just a lying provenance table). Both bar loaders put the
provenance insert inside the per-file transaction (`load_minute_bars.py:295-304`,
`load_daily_bars.py:317-325`); this loader should match. `cmd_fetch` in the actions loader has
the same shape (row committed up front, `row_count` NULL until the end, line 201-208/243-244) —
defensible there because the fetch is deliberately multi-transaction, but a crashed run leaves
`row_count` NULL; note it.

### S-5 — `ON CONFLICT DO NOTHING` on actions can silently drop real data

`load_corporate_actions.py:221-233` against `uq_corporate_actions (security_id, action_type,
ex_date)` (`005_corporate_actions.up.sql:92-93`). Two silent-drop cases:

1. **Provider revision.** A re-fetch after yfinance corrects a ratio (e.g. 0.25 → 0.2) hits the
   conflict and keeps the stale value, uncounted and unlogged. The loader cannot know the values
   differ without checking — and it doesn't.
2. **Genuine second action, same type, same ex_date.** A special dividend paired with a regular
   dividend on one ex-date is real-world-valid; the schema's uniqueness makes it
   unrepresentable (schema reviewer's call — the migration comment reasons only about *splits*,
   where the argument is sound), and the loader drops the second row without a trace. yfinance's
   Series likely pre-aggregates per date, which narrows the practical exposure, but the loader
   should not depend on that silently.

Minimum loader-side fix regardless of the schema ruling: when `rowcount == 0`, select the
existing row and WARN if the stored value differs from the fetched one.

### Probe 1 — resumability, traced (both bar loaders): SOUND

The claim holds under every failure point I could construct:

- **Kill mid-scan/mid-aggregate** (before the transaction opens): nothing written. Re-run
  re-hashes and retries. ✓
- **Kill between the `data_sources` insert and the COPY, or mid-COPY:** both live inside one
  `with conn.transaction()` (`load_minute_bars.py:295-323`, `load_daily_bars.py:317-348`) on an
  autocommit connection — psycopg issues a real BEGIN, and a killed session's in-flight
  transaction is rolled back by the server. The provenance row and the rows are atomic; there is
  no window where the hash is recorded without the rows or vice versa. ✓
- **`synchronous_commit = off` + OS crash:** the last commits may vanish — but they vanish
  *together* (row + provenance atomically), so resume-by-hash detects the file as absent and
  re-loads. The docstring's reasoning (`load_minute_bars.py:342-347`) is exactly right. ✓
- **Concurrent double-run (operator error):** both pass the hash check, both insert — the second
  COMMIT violates `uq_data_sources_artifact (provider, dataset, source_sha256)`
  (`001_core_schema.up.sql:96-98`), rolls back entirely, exits `EXIT_SQL`. No duplication. ✓
- **Same trading day re-loaded from different bytes** (e.g. a re-copied file): different hash, so
  the resume check passes — and the bar-table PK (`(security_id, ts)` / `(security_id,
  trade_date)`, `002_price_bars.up.sql:58,158`) aborts the COPY loudly. Correct outcome; the
  error message will be a raw PK violation rather than an explanation, which is acceptable.
- **Partial-skip semantics:** a file with unresolvable symbols or bad rows is recorded loaded
  with the *written* count (corrected inside the transaction —
  `load_minute_bars.py:322-323`, `load_daily_bars.py:348`), and the skips are counted, logged
  per-reason, and summarized. Deliberate and visible, not a hole.

### Probe 5 — `DayBar` aggregation: correct, one theoretical edge

Initial construction (`load_daily_bars.py:263`): `DayBar(ns, ns, open_, h, low_v, close, v)` —
the first row seen is simultaneously first and last, its open/close/high/low seed all four
fields. Correct. `update` (120-131) tracks open by strictly-earliest ns and close by
strictly-latest ns, so out-of-order arrival is handled and the docstring's claim holds. High/low
are strict max/min — correct. The only edge is two rows sharing an identical `window_start` for
one symbol: open/close arbitrarily keep the first-seen row, and `volume += v` double-counts
silently (N-8). I scanned a full 1.6M-row file for duplicate `(ticker, window_start)` pairs and
found zero, so this is defensive-depth, not an active bug.

### Probe 6 — session bounds from the filename: misnaming is detected

`session_bounds_ns` is computed from `trade_date_from_name` (filename). A file whose name
disagrees with its content has zero rows inside the computed session window, so `aggregate_file`
returns an empty dict and `load_file:302-303` raises
`LoadError: no regular-session bars found — refusing to record an empty day`, which stops the run
with `EXIT_VALIDATION`. Loud and correct. The residual case — a correctly-named file containing a
minority of rows stamped on a *different* date — silently discards those rows via the
`not (lo_ns <= ns <= hi_ns)` branch (line 241-242), indistinguishable from extended-hours
by design. Undetectable at this layer without a cross-check against rows_read distribution;
acceptable, but worth knowing the limit exists.

The empirical early-close evidence for the domain reviewer (S-6): on 2024-11-29 (13:00 ET close)
the archive prints the closing auction as a 13:00 bar (SPY: o=602.46 c=602.55 v=1,626,616) and
essentially nothing after (6,527 bars across 6,526 symbols in 13:00–15:59) — so the fixed 15:59
bound *correctly captures* half-day closes (stored SPY close 602.55 matches the auction). On the
regular day 2024-11-27, SPY has bars at 15:59 (c=598.79, stored as the daily close) *and* 16:00
(c=598.82) — the 16:00 auction bar is excluded by `SESSION_LAST_MINUTE`. Whether the daily
convention should include the 16:00 bar is a domain call, but the current choice is internally
inconsistent across day types: auction print included on half days, excluded on full days.

### Probe 7 — the gap-candidate query plan: acceptable

Ran live (`EXPLAIN (ANALYZE, BUFFERS)`, read-only): the window function walks
`price_bars_daily_pkey` via an Index Scan — the PK `(security_id, trade_date)` already provides
the `PARTITION BY security_id ORDER BY trade_date` ordering, so there is **no sort**; WindowAgg
streams 12,840,439 rows, filter passes 73,781, merge-join to securities, 96 s wall dominated by
2.6M buffer reads. No pathological plan, no index needed beyond what exists; for a run-rarely
admin command with `statement_timeout = 0` set in its own session (`connect:88`) this is fine.
(Note the `bin/db_psql.sh` session has a server-side statement_timeout that kills this query —
the loader is unaffected because it disables it per-session; anyone reproducing the analysis
interactively must `SET statement_timeout = 0` first.)

### Probe 10 — logging: clean

No DSN is ever logged; `DATABASE_URL` reaches the containers via `--env`, never argv
(`bin/db_load_bars.sh:56-58`, `bin/db_corporate_actions.sh:66-72`), so it does not appear in
`ps`. Connect-failure messages print the psycopg/libpq error, which carries host/user but never
the password. `data_sources.source_uri` stores local paths and the keyless FRED URL. Symbols,
prices, and dates are market data, not holdings — nothing sensitive for these four loaders.
`bin/db_psql.sh` keeps `PGPASSWORD` inside the container boundary. No findings.

---

## Coordination observations

For the schema/domain reviewers — facts established here that belong to their scope:

1. **(S-6)** The daily-close convention question, with the SPY 2024-11-27/2024-11-29 evidence
   above: 15:59-bar close vs 16:00-auction-bar close, and the half-day asymmetry. The
   `market_calendar` table already stores per-day `session_close` — if the ruling is "respect
   early closes / include the auction bar," the calendar loader's output is the natural input
   the daily loader currently ignores.
2. **(S-5)** `uq_corporate_actions` makes two same-type actions on one ex_date unrepresentable;
   the migration's comment argues splits only. Whether same-day regular+special dividends need
   representing is a schema decision; the loader-side silent drop is mine (S-5).
3. `fetch_actions` trusts yfinance's split-ratio convention (new/old, e.g. 4.0 for 4:1) without
   a sanity cross-check against the observed price gap it was selected by — the docstring's
   "provider is the source of truth" stance is coherent, but a gap-vs-ratio consistency warning
   would be nearly free at fetch time. Domain reviewer's call.
4. `adjust`'s `1e19` representability guard is correct against NUMERIC(30,10) (max < 1e20);
   the adjacent comment's "~1e13" ceiling claim is wrong (N-6) — flagged so nobody "fixes" the
   guard to match the comment.
5. The archive's 15 corrupt members (2024-12-10 → 2024-12-31) mean the 2024 year-end is absent
   from every derived series until re-copied; `load_reference_data report` correctly surfaces
   them as data gaps, not holidays — any consumer of the daily series should read that report's
   output before trusting December 2024.

## Constraints honored

Read-only review: all loader executions were `--dry-run`; all SQL was SELECT/EXPLAIN; no rows
written or deleted anywhere; no containers/volumes/networks created, stopped, or removed. `rh-db`
left healthy: migrations 001–006 applied, checksums ok (re-verified after the review).
