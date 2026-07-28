# Review: database schema (migrations 001-003)

Reviewer: independent senior review (PostgreSQL / quant data modelling). Scope: the six SQL files in
`db/migrations/` only (001–003, up and down). All findings below were verified against the live
database where verifiable; every experiment was run inside a transaction and rolled back — all three
migrations remain applied and the data state is untouched (confirmed: `schema_migrations` rows 001–003
present, DEFAULT partition empty, no test rows remain).

## Summary verdict: REQUEST CHANGES

The core point-in-time design is genuinely good — the `period_end` / `known_at` split, the provenance
model, and the delisted-retention stance are exactly right and better than most production quant
schemas I have reviewed. But two findings are broken-by-construction against the actual data
described in `DATA_INVENTORY.md`, and both were reproduced live: (B1) the fundamentals unique key
cannot represent restatements, which makes the most likely loader pattern a silent lookahead
injector; (B2) the documented partition-helper contract deterministically wedges ingest at the first
EST month boundary in the Polygon archive. Both are cheap to fix now and expensive after 300M rows
land.

## Bar checklist

| Bar item | Verdict | Note |
|---|---|---|
| §4.1 `timestamptz` everywhere, UTC | PASS | No naive timestamps anywhere. |
| §4.1 NUMERIC for money/prices, never float | PASS | See discussion in F5 — correctly applied, cost quantified. |
| §4.1 NOT NULL deliberate; CHECK for invariants | PASS | OHLC self-consistency checks are exemplary. |
| §4.1 FK explicit ON DELETE | PASS | RESTRICT/SET NULL choices are reasoned. |
| §4.1 / §4.4 every FK indexed | FAIL | Four `source_id` FKs unindexed (F4). |
| §4.2 name every constraint/index explicitly | FAIL (partial) | CHECKs/indexes named; FKs are auto-named (F6). |
| §4.3 created_at/updated_at + trigger on business tables | PARTIAL | Inconsistent across bar tables and `data_sources` (F7). |
| §4.4 validate proposed indexes with EXPLAIN on realistic data | FAIL | Impossible pre-load; `ix_fundamentals_screen` is doubtful (F8). |
| §4.5 correct, tested down migration | PASS | Verified by reasoning; ordering claim is correct (Q6 below). |
| §7.2 no lookahead (point-in-time data) | PARTIAL | Core modelling right; restatement gap (B1) and no enforcement point (F1). |
| §7.2 no survivorship bias (include delisted) | PARTIAL | Retention right; symbol-reuse gap (F3). |
| EVALUATION_FRAMEWORK §4 hard constraints | N/A | Both constraints target tables not yet built (see F9 / Coordination). |

## Findings

### BLOCKER
- **B1** — `uq_fundamentals_snapshot` omits `known_at`: restatements are unstorable, and the natural
  upsert loader silently overwrites as-first-reported with as-restated — a lookahead enabler in the
  one table whose stated purpose is preventing lookahead. Verified live. Corollary verified live:
  `DELETE FROM data_sources` can fail mid-flight via the `ON DELETE SET NULL` → COALESCE-unique
  collision. `003_fundamentals.up.sql:91-92`.
- **B2** — DEFAULT partition + the documented single-month `ensure_price_bar_partition()` contract
  deterministically wedges ingest at the first EST month-end in the Polygon archive (Nov 30 2020).
  Reproduced live: spillover row lands in DEFAULT, next month's partition creation then errors.
  `002_price_bars.up.sql:56-87`.

### SHOULD-FIX
- **F1** — Nothing enforces point-in-time query discipline; the naive
  `WHERE period_end <= :asof` query still leaks. Ship the canonical as-of accessor (view or SQL
  function) alongside the table. `003_fundamentals.up.sql:10-16, 94-97`.
- **F2** — `ck_securities_symbol` will reject real symbols present in a full-universe Polygon file
  (lowercase preferred forms, multi-part suffixes, test/digit tickers) — loud FK-chain load failures
  with no stated normalization policy. Verified live against the regex. `001_core_schema.up.sql:98`.
- **F3** — Global `uq_securities_symbol` cannot represent ticker reuse; over a 5-year full-universe
  backfill this reintroduces identity errors the delisted-retention design exists to prevent.
  `001_core_schema.up.sql:103`.
- **F4** — All four `source_id` FK columns are unindexed (Bar §4.1 P0); any `data_sources`
  delete/SET-NULL requires sequential scans of up-to-1.6B-row children. Either index them or
  document + enforce that `data_sources` is append-only. `001:91`, `002:29`, `002:104`, `003:74`.
- **F5** — `is_active` and `delisted_at` can contradict each other (`is_active = TRUE` with
  `delisted_at` set is representable); no CHECK ties them. `001_core_schema.up.sql:88-90`.
- **F6** — FK constraints are auto-named (`price_bars_minute_security_id_fkey` etc.), violating both
  Bar §4.2 and the file's own stated `fk_` convention. Verified in live catalog.
  `001_core_schema.up.sql:16` (claim) vs `001:91`, `002:20,29,94,104`, `003:20,74` (practice).
- **F7** — Audit-column policy is inconsistent and the deviations are undocumented:
  `price_bars_minute` has neither timestamp (defensible, unstated); `price_bars_daily` has
  `created_at` but no `updated_at`/trigger despite `adj_close` being a plausible later UPDATE;
  `data_sources` lacks `updated_at` despite mutable `row_count`/`notes`. `002:105`, `002:102-104`,
  `001:52`.
- **F8** — `ix_fundamentals_screen (peg_ratio, fcf_yield)` is unlikely to serve the real screen
  query shape (two range predicates; the screen must first restrict to latest-known-per-security).
  Unvalidated per Bar §4.4; likely dead weight. `003_fundamentals.up.sql:102-104`.

### NIT
- **N1** — `ck_fundamentals_known_at` compares against `period_end::timestamptz`, whose meaning
  depends on the session `TimeZone` GUC at insert time (live DB is UTC today; a client with a
  different GUC shifts the constraint boundary by hours). Prefer
  `known_at >= (period_end::timestamp AT TIME ZONE 'UTC')`. `003_fundamentals.up.sql:83`.
- **N2** — `ensure_price_bar_partition` checks `to_regclass('public.%I')` but creates the table
  unqualified — a non-default `search_path` could create the partition outside `public` and then
  never find it. Qualify the `CREATE TABLE` too. `002_price_bars.up.sql:65-69`.
- **N3** — `ck_price_bars_minute_hl (high >= low)` is implied by `ck_price_bars_minute_ohlc` plus
  `NOT NULL` (any open must sit between low and high, forcing low <= high). Harmless, but one of the
  two is redundant. `002_price_bars.up.sql:33-34`.
- **N4** — `first_seen` is nullable with no stated loader obligation; a point-in-time universe query
  cannot distinguish "listed before our data starts" from "never populated". Document the
  convention. `001_core_schema.up.sql:89`.

### PRAISE
- **P1** — The `period_end` / `known_at` split, with NULL-means-exclude semantics documented in both
  the header and column comments, is the correct point-in-time model — including the honest note
  that FMP as-restated history would make `known_at` unrecoverable (`003:1-16, 26-28, 114-116`).
  Do not let anyone "simplify" this to one date column.
- **P2** — `unparsed JSONB` preserving Bloomberg's `#N/A Invalid Field` / `#VALUE!` strings verbatim
  next to a NULL typed column (`003:67-72`) is exactly what DATA_INVENTORY §2 demands —
  "we don't know" stays distinguishable from "provider said something unreadable".
- **P3** — `data_sources` with `fetched_at` as the explicit pull-time anchor and `source_sha256`
  dedup (`001:36-65`) makes lookahead audits and re-ingest idempotency queries, not archaeology.
- **P4** — Delisted-retention with the survivorship-bias rationale written into the table comment
  (`001:73-76, 112-116`), and the daily-bars table existing specifically because Sharpe/Sortino read
  daily returns (`002:89-92`) — both decisions trace directly to the governing docs.
- **P5** — Honest down migrations: `migrate: destructive` annotations with explicit data-loss
  statements, including the irreplaceability of the Bloomberg 4-day sample (`003_fundamentals.down.sql:5-7`).

## Detailed findings

### B1 — Fundamentals unique key cannot represent restatements; the natural loader silently rewrites history
`003_fundamentals.up.sql:91-92`

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_fundamentals_snapshot
    ON fundamentals_snapshots (security_id, period_end, period_type, COALESCE(source_id, 0));
```

The key says: one row per (security, fiscal period, period type, source) — *forever*. But the same
source legitimately produces multiple readings of the same fiscal period at different times:
preliminary release, 10-Q filing, 10-Q/A restatement, later as-restated pulls. Verified live —
inserting a second row for the same (security, `2026-03-31`, `quarterly`, source) with a later
`known_at` fails with `duplicate key value violates unique constraint "uq_fundamentals_snapshot"`.

Why this is a BLOCKER and not a modelling nicety: the loader that meets this constraint will almost
certainly be written as `INSERT ... ON CONFLICT DO UPDATE`. That loader **silently replaces
as-first-reported values (and their early `known_at`) with as-restated values** — which is precisely
the "backtests over as-restated data inherit lookahead" failure mode this file's own header
(`003:14-16`) identifies as the thing to avoid. The alternative loader (`ON CONFLICT DO NOTHING`)
silently discards restatements instead, which corrupts the other direction. There is no correct
loader against this key. The schema's stated purpose — point-in-time by construction — is defeated
by its own uniqueness constraint.

Two verified corollaries:

1. **NULL source collision.** Two different pulls that both carry `source_id = NULL` collide on the
   COALESCE bucket `0` (verified live). Given the comment at `003:88-90` says disagreeing sources
   should *both* be kept, two distinct unsourced pulls failing hard contradicts the design intent.
   (Mechanically COALESCE-to-0 is safe against real ids — `data_sources.id` is `GENERATED ALWAYS`
   starting at 1 — but semantically it merges "unknown provenance" into one slot.)
2. **`DELETE FROM data_sources` can fail or corrupt.** The `ON DELETE SET NULL` on
   `fundamentals_snapshots.source_id` (`003:74`) rewrites the child's key expression to the `0`
   bucket. Verified live: deleting a source whose snapshot has an unsourced twin fails with a unique
   violation raised *from inside the SET NULL cascade*. Where no twin exists, the delete succeeds
   and silently merges distinct provenance into the NULL bucket — blocking future unsourced loads
   for that period.

**Fix direction:** make `known_at` part of the identity —
`UNIQUE (security_id, period_end, period_type, COALESCE(source_id, 0), known_at)` with a companion
partial unique for `known_at IS NULL` rows, or model an explicit `observed_at`/revision column.
Rows become append-only observations (which also makes the `updated_at` trigger on this table
nearly vestigial — a good sign). Reconsider `ON DELETE SET NULL` → `RESTRICT` on `source_id` at the
same time; provenance that outlives its source record is worth little.

### B2 — DEFAULT partition + single-month helper contract deterministically wedges ingest at real month boundaries
`002_price_bars.up.sql:56-87` (helper: 56-77; DEFAULT partition: 79-87)

The helper's documented contract is "call before loading a day" (`002:75-77`), and it creates
exactly one month's partition. But Polygon day files contain post-market bars through 20:00 ET, and
during EST (UTC−5) the 19:00–19:59 ET bars have `window_start` at 00:00–00:59 UTC **the next day**.
At an EST month-end — and the archive's coverage (2020-10-02 → 2021-08-30) contains five of them,
first on **Nov 30 2020** — those bars belong to the *next* month, whose partition does not exist
yet. They land in `price_bars_minute_default`.

Then the loader reaches the next month and calls the helper. Reproduced live:

```
INSERT ... ts='2021-01-01 00:30:00+00'  →  landed_in: price_bars_minute_default
SELECT ensure_price_bar_partition('2021-01-15');
ERROR:  updated partition constraint for default partition "price_bars_minute_default"
        would be violated by some row
```

PostgreSQL scans the DEFAULT partition when creating/attaching any new partition and refuses if any
resident row falls in the new range (taking an ACCESS EXCLUSIVE lock on the parent while doing so —
at scale, a scan of a large DEFAULT partition under that lock is its own outage). Ingest is now
wedged: no January partition can ever be created until someone hand-moves rows out of DEFAULT.
This is not a hypothetical loader bug — it is the documented usage pattern applied to the actual
data in DATA_INVENTORY §1, failing on the first EST month boundary, ~2 months into a 229-day load.

It also poisons the stated audit invariant: the comment at `002:85-87` says a non-empty DEFAULT
means "the loader produced out-of-range timestamps — investigate". Under the current contract,
perfectly valid bars land there routinely, so the alarm the DEFAULT partition exists to provide
cries wolf from week one.

**Fix direction (any one of):** (a) have the helper take the file's actual `min(ts)..max(ts)` range
(or simply always create month N and N+1); (b) pre-create all partitions for the known archive
window in this migration — 11 months is 11 DDL statements, and 5 years is 60; (c) drop the DEFAULT
partition entirely and let a genuinely out-of-range row fail loudly per Bar §0 "fail loud" — with a
staging-table load pattern, the abort-mid-COPY objection at `002:80-82` disappears. I would do (b) +
(a); keep DEFAULT only if the loader gains an explicit "ensure both boundary months" obligation in
its contract comment.

### F1 — No enforcement point for point-in-time queries
`003_fundamentals.up.sql:10-16, 94-97`

The comments say "every point-in-time query filters on known_at, never period_end" — but a comment
is not an enforcement mechanism, and the naive query
`SELECT ... WHERE period_end <= :asof ORDER BY period_end DESC LIMIT 1` runs happily and leaks. To
the specific question of whether nullable `known_at` is a trap: **for the correct filter it is
actually safe** — `WHERE known_at <= :asof` excludes NULLs by three-valued logic, which is exactly
the documented must-exclude semantics; the trap is the consumer who writes
`COALESCE(known_at, period_end) <= :asof` "to use more rows", or skips `known_at` altogether.
EVALUATION_FRAMEWORK §3.5 demands leakage be "enforced in the feature store, not by convention" —
the schema layer's contribution should be a canonical accessor: a
`fundamentals_asof(security_id BIGINT, as_of TIMESTAMPTZ)` SQL function (or view) that pins the
`known_at` filter and rides `ix_fundamentals_pit`, with backtest code reviewed against "reads the
accessor, never the raw table". Cheap now; per the framework's own words, impossible to retrofit
after consumers exist.

### F2 — Ticker CHECK rejects real full-universe symbols with no stated normalization policy
`001_core_schema.up.sql:98`

`^[A-Z]{1,5}([.-][A-Z]{1,2})?$` admits BRK.B and BF-B (verified live) — good. But DATA_INVENTORY §1
says the Polygon files are "the full US equity universe, not a watchlist" (1.44M rows/day), and that
universe includes symbol forms this regex rejects (verified live): lowercase-convention preferreds
(e.g. `BACpA`), multi-part suffixes (e.g. `AAIC.PR.B`, warrant classes like `X.WS.A`), and
test/digit symbols. Consequence: the securities upsert for those symbols raises a CHECK violation,
and every bar row for them then fails the FK — a loud failure, so not a blocker, but the load
pipeline cannot be written until someone decides: normalize to a canonical grammar, widen the CHECK,
or explicitly skip non-common-stock instruments (defensible, but then *skipping must be a logged
decision*, not an exception trail). Extract `SELECT DISTINCT ticker` from one real day file and test
the regex against it before the loader is written — that turns this from speculation into a policy.

### F3 — Global symbol uniqueness cannot represent ticker reuse
`001_core_schema.up.sql:103`

`uq_securities_symbol` on `(symbol)` means a recycled ticker — common over multi-year horizons; the
delisted name's symbol gets reassigned to an unrelated company — must either overwrite the old row's
identity or be unrepresentable. Both corrupt point-in-time universe reconstruction: bars for the old
company and the new one attach to the same `security_id`, which is survivorship bias's uglier
sibling. For the 11-month archive this is unlikely to bite; for the stated 5-year backfill it will.
Fix direction: partial unique `ON securities (symbol) WHERE delisted_at IS NULL` (one *live* holder
of a symbol) plus a documented loader rule that a re-listed symbol is a *new* row; symbol→id
resolution for historical loads then goes through `(symbol, as-of-date)` against
`first_seen`/`delisted_at`.

### F4 — Unindexed `source_id` FKs on tables sized in the hundreds of millions
`001_core_schema.up.sql:91`, `002_price_bars.up.sql:29,104`, `003_fundamentals.up.sql:74`

Bar §4.1 [P0]: every FK is indexed. Verified in the live catalog: none of the four `source_id`
columns has an index. Any `DELETE FROM data_sources` must sequentially scan every child — at target
scale, a scan of 1.6B minute rows per deleted source row, under row locks. The pragmatic defense
("we never delete sources") is real, and indexing `source_id` on the minute table costs tens of GB
for a column with pathological selectivity — so I would *not* blindly add four indexes. Instead:
change `source_id` FKs to `ON DELETE RESTRICT` (RESTRICT still scans, but converts "slow surprise
cascade" into "fast refusal" only when a delete is attempted), document `data_sources` as
append-only, and index `source_id` only on `fundamentals_snapshots` (small, and "show me everything
this pull produced" is a genuinely useful audit query). Whatever the choice, the deviation from a
P0 rule must be written down in the migration, not discovered by the next reviewer.

### F5 — `is_active` and `delisted_at` can contradict
`001_core_schema.up.sql:88-90`

Nothing prevents `is_active = TRUE, delisted_at = '2021-03-02'`. Two columns encoding overlapping
truths without a CHECK is how reference data rots. Add
`CONSTRAINT ck_securities_active_delisted CHECK (delisted_at IS NULL OR NOT is_active)` — or drop
`is_active` entirely and define active as `delisted_at IS NULL` (adjusting `ix_securities_active`'s
predicate accordingly), which removes the second source of truth altogether.

### F6 — FK constraints auto-named, contradicting the file's own convention
`001_core_schema.up.sql:16` declares `ix_ / uq_ / fk_ / ck_ naming`; the live catalog shows
`price_bars_minute_security_id_fkey`, `fundamentals_snapshots_source_id_fkey`, etc. Every CHECK,
index, and unique got an explicit name; every FK did not (`001:91`, `002:20,29,94,104`,
`003:20,74`). Bar §4.2 [P0] exists because auto-names diverge across environments and break
migration diffs. Trivial fix now (`CONSTRAINT fk_price_bars_minute_security REFERENCES ...`);
annoying rename choreography after the tables are referenced by tooling.

### F7 — Audit-column policy inconsistent and deviations undocumented
`002_price_bars.up.sql:19-42` (minute: no created_at/updated_at), `002:105` (daily: created_at
only, no trigger), `001:52` (data_sources: created_at only). Bar §4.3 [P0] says every business
table gets both plus the trigger. Omitting them on `price_bars_minute` is the right call — 8 bytes
× 1.6B rows ≈ 13 GB per column for immutable bulk data whose load provenance already lives in
`data_sources.fetched_at` via `source_id` — but a deliberate P0 deviation must say so in the
migration comment, which currently argues partitioning at length and never mentions the audit
columns. Meanwhile `price_bars_daily.adj_close` is documented as arriving "when the provider
supplies one" (`002:100-102`) — i.e. plausibly by later UPDATE — with no `updated_at` to show it
happened. Either declare both bar tables append-only-immutable (and make re-loads delete+insert
under a new `source_id`), or give `price_bars_daily` the standard pair + trigger.

### F8 — `ix_fundamentals_screen` unlikely to match the screen's real query shape
`003_fundamentals.up.sql:102-104`

A btree on `(peg_ratio, fcf_yield)` serves a range predicate on the *leading* column only;
`WHERE peg_ratio < :x AND fcf_yield > :y` scans the peg range and filters fcf inside it. Worse, the
real screen (per the PIT design) is "for each security, take the latest row with
`known_at <= now()`, *then* filter ratios" — a DISTINCT ON / lateral over `ix_fundamentals_pit`, in
which this index participates not at all. It was also necessarily merged unvalidated against
realistic data (Bar §4.4 [P0] — no data exists yet). Recommendation: drop it from the migration and
re-propose with an `EXPLAIN (ANALYZE, BUFFERS)` once the Bloomberg sample is loaded and the actual
screen SQL exists. Indexes are easy to add and politically hard to remove.

## Answers to the specific evaluation questions

1. **PIT correctness** — modelling sufficient (P1), enforcement absent (F1), restatement identity
   broken (B1). Nullable `known_at` is safe under the correct filter, dangerous under COALESCE.
2. **Partitioning** — monthly RANGE is right-sized (~30M rows/month at full universe); PK
   `(security_id, ts)` is correct for "symbol over window" with pruning, and global uniqueness
   holds because the partition key is in the PK. `ix_price_bars_minute_ts` is a genuine partitioned
   index — verified it cascaded to `price_bars_minute_default_ts_idx` — and does what the comment
   claims. The DEFAULT partition is the footgun: B2.
3. **Sizing** — measured live: a fully-populated tuple is 96 bytes; NUMERIC(18,6) prices measure
   10 bytes each vs 8 for float8. At 1.6B rows: ≈160 GB heap + ≈65 GB PK + ≈40 GB ts index; the
   NUMERIC premium is roughly 13 GB total — immaterial. Arithmetic on NUMERIC is several-fold
   slower, but heavy math will happen in numpy after extraction anyway, and Bar §7.2 [P0] names
   *prices* explicitly. The "never float" rule is correctly applied, not over-applied; DOUBLE would
   be defensible in a research-only store, but this store also feeds P&L against real fills.
   Monthly partitions are not too coarse.
4. **Constraints** — all four OHLC columns NOT NULL, so `BETWEEN` has no NULL hazard;
   `ck_price_bars_minute_hl` is technically implied by `_ohlc` (N3). The symbol regex admits BRK.B /
   BF-B but wrongly rejects real full-universe forms (F2).
5. **COALESCE(source_id, 0)** — mechanically sound against real ids (identity starts at 1), but two
   NULL-source pulls collide, and `ON DELETE SET NULL` can blow up or silently merge provenance —
   both verified live. Folded into B1.
6. **Down migrations** — they truly reverse. The ordering comment in `001_core_schema.down.sql:13-15`
   is correct for the runner's descending rollback. A hypothetical rollback of *only* 001 with 002/003
   still applied fails **loudly and safely**: `DROP TABLE securities` (no CASCADE) errors on the
   dependent FKs before anything is lost, and `DROP FUNCTION set_updated_at()` (no CASCADE) would
   likewise refuse while 003's trigger exists. Deliberate use of non-CASCADE drops as a safety
   property — good.
7. **Missing tables** — deferral is legitimate. `agents`/`debates`/`paper_portfolios`/etc. will key
   off `securities` and read `price_bars_daily`; nothing in 001–003 forces rework when they arrive.
   The two EVALUATION_FRAMEWORK §4 hard constraints (`n_observations NOT NULL`; as-of timestamps on
   every metric row) bind on `evaluation_runs`, which does not exist yet — they must be carried
   forward as acceptance criteria for that migration (see Coordination).
8. **Audit columns** — the minute-table omission is justified but undocumented; daily and
   `data_sources` are inconsistent (F7).
9. **Indexing** — screen index doubtful (F8); `source_id` FKs unindexed (F4);
   `ix_fundamentals_pit` is exactly the right index for the hot PIT lookup;
   `ix_fundamentals_period` and `ix_data_sources_fetched_at` are plausible but also unvalidated.
10. **Delisted/survivorship** — retention model is right; symbol reuse (F3), the
    `is_active`/`delisted_at` contradiction (F5), and undocumented nullable `first_seen` (N4) are
    the gaps between "keeps delisted rows" and "can reconstruct the tradeable universe on date D".

## Coordination observations

- **For the loader owner (out of my scope but load-bearing here):** B1 and B2 are both traps whose
  tripwire is in the loader. Whatever fix lands, the loader contract must state (a) the upsert rule
  for fundamentals revisions and (b) the partition-ensure obligation covering the file's true ts
  range, not its nominal date.
- **For `db/migrate.py`'s reviewer:** the 001 down-file safety argument depends on the runner
  rolling back in strictly descending version order and on non-CASCADE drops failing the transaction
  — worth confirming the runner really does refuse/abort on such an error rather than continuing.
- **For the future 004+ (evaluation tables):** carry the two EVALUATION_FRAMEWORK §4 hard
  constraints as explicit acceptance criteria; nothing in 001–003 records them anywhere a migration
  author would trip over.
- **DB role hygiene (Bar §4.9, observed while testing):** the review connection could freely
  INSERT/DELETE/CREATE — fine for a dev box, but the eventual runtime role should be DML-only,
  distinct from the migration (DDL) role.
