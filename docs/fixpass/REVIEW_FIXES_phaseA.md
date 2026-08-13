# Review: Phase A fix-pass

**Reviewer:** independent re-review — quantitative-finance / data-engineering scope. I did not write
this code, did not perform either original review, and did not perform the fix-pass.
**Date:** 2026-07-29
**Inputs:** `SENIOR_ENGINEER_BAR.md` §0/§4/§7.2 · `REVIEW_phaseA_loaders.md` (2 BLOCKER / 7 SHOULD-FIX)
· `REVIEW_phaseA_semantics.md` (5 BLOCKER / 9 SHOULD-FIX) · `FIX_REPORT_phaseA.md` ·
`EVALUATION_FRAMEWORK.md` §2/§3.5 · `db/migrations/007_point_in_time.up.sql` + down · all five
loaders · `db/verify_daily_series.py` · both new test files.

**Method — what I executed rather than read.** I treated `FIX_REPORT_phaseA.md` as a set of claims to
falsify, not as evidence.

- **Independent recomputation of the entire adjustment.** All 1,648,849 bars of split-bearing
  securities, factor recomputed with a *temporary exact-numeric product aggregate*
  (`CREATE AGGREGATE pg_temp.numprod(numeric) (SFUNC=numeric_mul, ...)`) — deliberately **not** the
  `exp(sum(ln()))` identity the shipped function uses, so the check is method-independent.
- **Provider cross-check** of 8 symbols × 1,241 sessions (yfinance from the `rh-egress` container),
  including both NVDA splits, AGZD, and SPY.
- **Five defences reverted on a scratch copy** of `db/` (never the working tree) and the named tests
  re-run; every scratch file restored and `diff`-confirmed byte-identical.
- **Two blockers reproduced live end-to-end** against the real thing: B-1 by standing up an
  `--internal` Docker network with no egress and a throwaway Postgres, and running the actual
  `rh-actions` image against it; B-2 by running the actual minute loader over
  `data/market/minute_bars_5y/2024/12`, which contains 6 of the archive's 15 corrupt members
  including the mid-stream `zlib.error` case.
- **007's down migration** exercised on the throwaway database (001→007 applied, down to 006, re-up),
  never on `rh-db`.
- **`bash bin/local_test.sh`** run in full.

**Constraints honoured.** No `docker prune`. No `km-*` object touched — all 10 verified `Up (healthy)`
after. `rh_db_data` untouched; `data/market/` mounted read-only in every container; no
INSERT/UPDATE/DELETE/DDL against `price_bars_daily`, `corporate_actions`, `market_calendar`,
`risk_free_rates`, `securities`, or `price_adjustment_state` on `rh-db`. No rollback past 006 on the
live database. `rh-db` left `running healthy`, 001–007 applied, checksums `ok`, 12,840,439 bars.
Scratch containers/network (`rhrev-pg`, `rhrev-noegress`) created and removed. `git status` is
byte-identical to arrival except this file.

---

## Summary verdict: PASS WITH CONDITIONS

**The blocker that mattered most is genuinely, verifiably closed.** B-S1 is not merely claimed — I
recomputed every one of 1,648,849 split-security factors by an independent exact-numeric method
bounded at the declared as-of and got **0 mismatches, max absolute difference 0.000000000000**; all
11,191,590 non-split bars carry factor exactly 1; `adj_close = round(close/factor, 10)` holds on
every non-NULL row; `split_factor_after` no longer exists in `pg_proc`; PAVS 2025-09-30 reads
`$1.0400000000`. The design decision (drop the unbounded function rather than deprecate it, pin the
materialised columns to a recorded singleton) is the right one, and the reasoning that returns were
never contaminated while levels were is correct and now documented in the catalog.

Four of the seven blockers are fully FIXED and I could not break any of them. Every revert I ran went
red for the right reason, including the B-S4 pin the fix-pass admits it got wrong on the first
attempt — the re-anchored version fails loudly on the forbidden change, and a *second* independent
test fails too.

**Three things stop this being a clean PASS.**

1. **NEW BLOCKER — `verify_daily_series.py --provider` is broken and fails on a correct database.**
   The module the fix-pass built as its S-S6 deliverable compares our **raw** `close` against the
   provider's **split-adjusted** close. Line 214 binds `c` to `close` and discards the `adj_close`
   it just fetched (`_a` is unused). I ran it: `check 2: NVDA: close vs official worst 390813.8 bps
   (bound 100.0), mean +115262.338 bps (bound ±1.0)` → **exit 1**. Checks 3 and 4 — the paired-return
   sd and realized-vol tests that were the *whole point* of the review's sharper test, the ones that
   "map directly onto the Sharpe error" — are therefore dead for every in-window split name, which is
   the only cohort where an adjustment error can exist. This is worse than a missing check: a
   verification tool that red-lights a correct database teaches its operator to ignore it, or to
   "fix" it by loosening the bound. The fix report's S-S6 "FIXED" row cannot have been executed as
   documented.

2. **B-S5 is PARTIALLY-FIXED and the documented tripwire does not work.** The >180d and >120d classes
   are genuinely closed (0 gap-returns above 120 days remain in 12.8M bars). But **448 securities
   still carry a 60–120 day internal hole**, 23 of them producing a ≥200% "single-session" return.
   I checked five against yfinance and **all five are real identity breaks, not halts**: COHR (old
   Coherent Inc \$266.29 → II-VI-renamed Coherent Corp \$43.19, and II-VI's four splits are attached
   to the combined id), FIG (Figure Acquisition → Figma), DBD, VRM and FNGU (provider history begins
   at exactly our post-gap date in each case). `FOLLOW_UPS.md` says "the alignment check is the
   tripwire" for these. It is not: `REFERENCE_SYMBOLS` is 20 mega-caps, which is precisely the cohort
   where tickers are never recycled. META was caught only because it happens to be on that list.

3. **B-S3 is PARTIALLY-FIXED and the fix report says otherwise.** The catalog is corrected, correctly
   and via 007 rather than by editing an applied file. But the fix report claims "the documented
   formula no longer mixes bases **anywhere**", and the mixed-basis formula survives verbatim in two
   migration files: `004_evaluation.up.sql:484` (`-- Σ shares x adj_close + cash.`, the closing line
   of the reasoning block a future marking-job author will read) and
   `005_corporate_actions.up.sql:18` (`this system marks portfolios as \`Σ shares × adj_close +
   cash\``, load-bearing on 005's central dividend argument). Neither is a `COMMENT ON`, so the
   checksum argument does not apply to them as an excuse — 007 could have cited them, and the report
   should not have said "anywhere".

Given that **nothing consumes any of this yet** (`grep` across `backend/`, `src/`, `frontend/` for
`adj_close|sharpe|risk_free|return_basis` returns nothing), there is no live damage and all three are
cheap to close now. Gates are green: all six suites PASS, 148 database tests.

---

## Finding-by-finding verification

### Loaders review

| ID | Orig sev | Status | Notes |
|---|---|---|---|
| **B-1** | BLOCKER | **FIXED** | Reproduced live on an `--internal` no-egress network with the real `rh-actions` image. One failing symbol → **exit 1** with `ERROR … 1 symbol(s) could NOT be asked (provider failures): AAPL … Exiting non-zero: an incomplete fetch must not read as success.` 26 symbols, all failing → **exit 3** after burning exactly **15** calls (`CONSECUTIVE_PROVIDER_FAILURES_ABORT`, `load_corporate_actions.py:101`) with a correct diagnosis naming "no egress? wrong wrapper? hard rate limit". "No actions" still exits 0. The three ways the old code failed (DEBUG level, uncounted, exit 0) are each closed. `load_corporate_actions.py:298-315, 366-373`. Note: no candidates at all still exits 0 (`:272-274`) — correct, and distinguishable by the warning. |
| **B-2** | BLOCKER | **FIXED** | Run against the real archive, not a fixture: `load_minute_bars.py --root /repo/data/market/minute_bars_5y/2024/12 --limit 12` loaded 6 healthy files (9,607,994 rows) and survived **2024-12-10** — the mid-inflate `zlib.error` that killed the old loader — plus 5 header-level `BadGzipFile` members, then **exit 1** with all six named and the "Those trading days are ABSENT … re-copy them" instruction. `CORRUPT_STREAM_ERRORS` tuples in the two bar loaders are identical; both guard `gzip.open`, the header read, and the row iteration. |
| S-1 | SHOULD-FIX | **FIXED** | Widened tuple `(OSError, EOFError, zlib.error, UnicodeDecodeError, csv.Error)` shared by both loaders, applied at all three sites; `sha256_of` OSError folded in. Unit-tested across all five types. |
| S-2 | SHOULD-FIX | **FIXED** | Verified by reading the COPY binding: `cp.write_row((sec_id, trade_date, b.open, b.high_s, b.low_s, b.close, …))` against column list `(…, open, high, low, close, …)` — all four OHLC fields are provider source strings; floats are comparison keys only. The pre-screen is now exact `Decimal`, so it can no longer be weaker than the CHECK it fronts. |
| S-3 | SHOULD-FIX | **FIXED** | 4 attempts, `FetchError → EXIT_CONNECTION` (3), caught before its parent `LoadError`. See NIT-6 below on the "capped" wording. |
| S-4 | SHOULD-FIX | **FIXED** | `data_sources` row and rate rows in one transaction; `row_count` = rows inserted. |
| S-5 | SHOULD-FIX | **FIXED** | `_warn_if_conflict_differs` re-reads and value-compares with `math.isclose`; WARNs with both values, counted in the summary, stored value deliberately not overwritten. Regression-tested (`test_s5_conflicting_action_value_warns`) and I reverted nothing here because the test asserts both the warning *and* the non-overwrite. |
| S-6 | SHOULD-FIX | **FIXED per the B-S4 adjudication** | Implemented exactly as ruled: `SESSION_LAST_MINUTE` untouched, documentation corrected, behaviour pinned. |
| S-7 | SHOULD-FIX | **FIXED** | 22 unit + 9 blocker-regression tests. Suite 117 → 148, confirmed by my own run. |
| N-1 … N-5, N-7, N-8, N-9 | NIT | **FIXED** | Spot-verified: `Decimal(raw)/100` (no float); `_open_csv -> tuple[io.TextIOBase, Iterator[list[str]]]`; `positive_int` argparse; `OperationalError → EXIT_CONNECTION` before the general handler in all loaders; guarded `import yfinance`; separate minute-row vs day-bar counters; the ≤390-bit minute bitmask with duplicate skip-and-count. N-9's reversal (all selectors resolve to live holders only) is **correctly argued and I agree with it** — asking yfinance about a delisted identity is exactly the B-S5 mis-attribution, and the excluded count is reported with the rationale in code. |
| N-6 | NIT | **FIXED, superseded by S-S9** | The "~1e13" claim is gone; `MAX_MEANINGFUL_ADJ_CLOSE = 1e12` with a comment stating the true `NUMERIC(30,10)` ceiling (<1e20) and that the cutoff is semantic. |

### Semantics review

| ID | Orig sev | Status | Notes |
|---|---|---|---|
| **B-S1** | BLOCKER | **FIXED (materialised columns) · residual quantified below** | Independently recomputed: **1,648,849 bars checked, 0 mismatches, max abs diff 0** against an exact-numeric product bounded at `adjustment_as_of = 2025-10-02`. Non-split bars with factor ≠ 1: **0**. `adj_close ≠ round(close/factor,10)`: **0**. `split_factor_after` in `pg_proc`: **0** — dropping it rather than deprecating it is the right call and makes misuse structurally impossible per §3.5. PAVS/WOK/AREB/HUBC/OCG/VSA on 2025-09-30 all read raw close with factor 1. `price_adjustment_state` singleton = `(1, 2025-10-02)`, written before factors. Max stored level fell 2.63e12 → **2.658e8**. The `announced_at` resolution is sound reasoning, not hand-waving: ex-date bounding uses only publicly-happened events. One wording quibble — `adjustment_as_of` is *derived* (`max(trade_date)`, `load_corporate_actions.py:428`), not "declared" by an operator; it is recorded and auditable, so the substance holds. |
| **B-S2** | BLOCKER | **FIXED (schema) / DEFERRED-WITH-DOC (data)** | Verified in `information_schema`: `return_basis` and `rf_conversion` are `is_nullable=NO` with `column_default` **empty** — nothing supplies a default. `ck_evaluation_runs_return_basis` closes the vocabulary to `('price_only','total_return')`; the test proves `'total'` is rejected too, so there is no vaguer third label. **Judgement: the schema guard IS sufficient as an interim, not merely a label** — the table is empty, nothing writes to it, and NOT-NULL-without-default is the only mechanism that makes the mislabel unstorable rather than merely discouraged. The 5% dividend coverage is unchanged (960/19,713 securities, SPY zero — I re-measured) and correctly listed in `FOLLOW_UPS.md`. |
| **B-S3** | BLOCKER | **PARTIALLY-FIXED** | Catalog is correct and the mechanism is sound — re-issued `COMMENT ON` in 007 rather than editing applied 004/005, which is exactly right (editing them raises `ChecksumMismatch` by design; I confirmed the checksum gate is real by cycling 007 down/up on a throwaway DB). `paper_portfolio_positions` now says `Σ shares × RAW close + cash` with the ex-date share-count rule and `NEVER mark with adj_close`; `adj_close`'s comment ends `NEVER a marking price`. **But the wrong formula still stands in `db/migrations/004_evaluation.up.sql:484` and `db/migrations/005_corporate_actions.up.sql:18`** — plain SQL comments, not `COMMENT ON`, so no checksum argument protects them from at least a cross-reference. The fix report's "no longer mixes bases **anywhere**" is untrue. See New findings SF-1. |
| **B-S4** | BLOCKER | **FIXED** | `SESSION_LAST_MINUTE = dtime(15, 59)` untouched, correctly. **Pin genuinely revert-red:** I set it to `dtime(16, 0)` on the scratch copy and `test_bs4_daily_close_is_the_1559_bar_by_design` failed with stored close `544.300000`, high `548.620000`, volume `5863474` — the post-close print, i.e. exactly "a different wrong number". The re-anchoring is real: the pin derives its bucket timestamps from `datetime(2025,4,9,15,59, tzinfo=ldb.EXCHANGE_TZ)`, absolute wall-clock, not from the constant under test. A **second** independent test also went red (`test_session_bounds_follow_dst`, `389 * 60 * 1e9`), so the change is double-guarded. **Documented bounds match reality exactly** — I reproduced all four against yfinance from scratch: 6.849% of 1,241 SPY closes within \$0.005; worst 95.69 bps; mean signed −0.165 bps; volume median 0.853. Also reproduced the review's per-symbol means to 3 decimals (NVDA −0.024, GOOGL +0.091, AMZN +0.024, WMT +0.134, TSLA −0.534). |
| **B-S5** | BLOCKER | **PARTIALLY-FIXED** | >120d class **closed**: 0 gap-returns above 120 days remain across 12.8M bars; 19,713 securities (19,301 + 289 + 123) and 8,033 delisted reconcile exactly to the claimed splice counts. META verified real and fixed: two identities, id 1832322 (Roundhill ETF, 2021-06-30 → 2022-01-28, `delisted_at=2022-01-29`, **0 actions**) and id 12893123 (Meta Platforms, 2022-06-09 → 2025-10-02, **10 actions**) — the +1,396% was real and is gone. FB spliced the same way. Splice mechanics are sound (per-security transaction, `delisted_at` set before the successor insert so the partial unique index does not reject it, `SPLICE_BLOCKING_REFS` refuses securities with scored history, actions follow the current holder). **The 60–120d cohort is the gap** — see the dedicated section below. |
| S-S1 | SHOULD-FIX | **FIXED** | `expected_sessions INTEGER NOT NULL`, verified against `market_calendar` by `trg_evaluation_runs_n` (`AFTER INSERT OR UPDATE`, confirmed in `pg_get_triggerdef` — the UPDATE half matters and is present); zero-calendar-coverage windows refused outright; `CHECK (expected_sessions >= 1 AND n_observations <= expected_sessions)`; `coverage_ratio` generated stored. The extended `test_n_observations_is_verified` proves a truthful `n` with a false `expected_sessions` is unstorable. The `NUMERIC` (uncapped) choice for `coverage_ratio` is correctly reasoned in the migration — generated columns compute before CHECKs, so a capped type would replace a named CHECK with a confusing overflow. |
| S-S2 | SHOULD-FIX | **FIXED** | `rf_conversion` NOT NULL, CHECK-closed to `('simple','compound','annual')`, all three formulas spelled out in both the DDL and the column comment, with the 7% Sharpe swing cited. |
| S-S3 | SHOULD-FIX | **FIXED** | `FRED_SERIES = "DGS3MO"`; 11,225 observations loaded; 29,358 rate rows total (18,133 DTB3 retained + 11,225). Retaining DTB3 is right — series is part of the identity. See NIT-2 on the "all one direction" overclaim. |
| S-S4 | SHOULD-FIX | **FIXED** | `risk_free_rates` PK is `(series, effective_date, known_at)` (`004:86`), the `ON CONFLICT` targets that PK, and `ck_risk_free_rates_known` permits a fetch-time `known_at` — so a revision is representable. Keeping `effective_date + 1d` as the *floor* for first observations is the right call and better than the review's suggestion: a fetch-time stamp on a 45-year backfill would hide every historical rate from every historical decision. The self-criticism was correctly replaced with the review's "conservative" analysis. |
| S-S5 | SHOULD-FIX | **FIXED** | `DATA_INVENTORY.md` re-scoped to "curated-watchlist backtest, not a universe backtest" with the limits enumerated. |
| S-S6 | SHOULD-FIX | **NOT-FIXED / REGRESSION-INTRODUCED** | Checks 1 and 5 exist and pass offline (I ran them: 20/20 symbols align; every in-scope factor matches). **Checks 2/3/4/6 do not work** — raw vs split-adjusted comparison, exit 1 on a correct database. New BLOCKER B-N1 below. Additionally, check 1's docstring claim "a one-session offset fails immediately and cannot pass by luck" is **REFUTED** (see the untrue-claim audit) and check 5's scope claim is overstated. |
| S-S7 | SHOULD-FIX | **FIXED (doc) / DEFERRED-WITH-DOC (population)** | `first_seen` comment states the left-censoring concretely; I re-measured 8,678 rows at 2020-10-02 and confirmed name/exchange/security_type/sector/industry are 100% NULL. Deferral is justified (per-symbol FMP profile ≈ 14,600 calls). |
| S-S8 | SHOULD-FIX | **REJECTED-WITH-RATIONALE, and I agree** | The rejection reasoning is correct and I checked its premises: a `DEFAULT 1` would bake in exactly the silent lie S-S8 warns about, and bars must land before `adjust` runs so a plain NOT NULL blocks loads. `cmd_adjust` does return `EXIT_VALIDATION` while any factor is NULL (`:555-557`) — verified in code and live (`factor_null = 0`). `verify` refuses to pass without `price_adjustment_state` (`:445-450`) — verified. The residual (a consumer reading `close` directly) is correctly escalated to the marking-job spec. |
| S-S9 | SHOULD-FIX | **FIXED** | `MAX_MEANINGFUL_ADJ_CLOSE = 1e12` applied in the adjust UPDATE's `CASE`; the NULL path is now reachable and unit-tested. The honest statement that it currently NULLs 0 live rows *because* the trillion-dollar levels were themselves lookahead artefacts is exactly the right way to report it. |
| N-S1 | NIT | **DEFERRED-WITH-DOC** | Rename deferral is reasonable and correctly ticketed; nothing consumes the column. |
| N-S2, N-S3 | NIT | **FIXED** | Gap-band comment names the 5-for-4 and sub-33% misses; `AD_HOC_CLOSURES` lists 9/11 and Sandy. See NIT-5 — the closure list is incomplete in the *nearer* direction. |

---

## Regression-test verification (defence reverted → tests that went red)

Baseline first: the 9 tests in `test_loaders_db.py` pass on an untouched scratch copy (`9 passed in
17.57s`). Then, one revert at a time, scratch copy only, every file restored and `diff -q`-confirmed:

| ID | Revert applied | Test run | Result |
|---|---|---|---|
| **B-S1** | `AND ex_date <= p_as_of` deleted from 007's `split_factor_between` | `test_bs1_post_asof_splits_are_excluded` | **RED** — `(2025-09-29, 0.020000000000, 104.0000000000)` against the expected `(2.0, 1.04)`. The PAVS contamination reproduced to the cent. |
| **B-S4** | `SESSION_LAST_MINUTE = dtime(16, 0)` — the exact change the review forbids | `test_bs4_daily_close_is_the_1559_bar_by_design` | **RED** — `(544.300000, 548.620000, 5863474)` vs `(543.37, 544.56, 4638786)`. Also **RED**: `test_loader_units.py::test_session_bounds_follow_dst`. The pin is genuinely re-anchored; the fix-pass's self-reported first-draft bug is fixed. |
| **B-S2** | `return_basis TEXT NOT NULL` → `TEXT` | `test_bs2_return_basis_is_required_and_closed` | **RED** — `Failed: DID NOT RAISE NotNullViolation`. |
| **B-S5** | splice gap detection neutered (`WHERE FALSE AND …`) | `test_bs5_splice_and_infer` | **RED** — `assert 1 == 2`; the recycled ticker stayed one identity. |
| **B-1** | *not reverted — reproduced the real failure instead* | live, no-egress `--internal` network, real `rh-actions` image | exit **1** (one symbol) / exit **3** (all symbols, 15 calls burned). Old behaviour was exit 0 and silence. |
| **B-2** | *not reverted — reproduced the real failure instead* | live, real archive `2024/12` incl. the mid-stream `zlib.error` member | exit **1**, 6 corrupt members named, 6 healthy files loaded (9.6M rows), run **completed**. Old behaviour was an uncaught traceback on the first one. |

All four scratch reverts restored byte-identical. An incidental positive: putting a `.bak` file inside
`db/migrations/` made `migrate up` **refuse to run** ("Refusing to skip it: a silently ignored file
would let `up` report success while this migration never runs") — an unadvertised defence working
correctly.

**Verdict: the regression tests are real.** They fail for the right reason with the right values, and
in two cases a second test fails independently. I could not find a test that passes vacuously.

### Migrations 001–006 and the 007 down migration

- **`007` down/up cycles cleanly** on the throwaway DB: `down --target 006 --allow-destructive` →
  `split_factor_between` gone, `split_factor_after` restored, `price_adjustment_state` dropped, all
  four `evaluation_runs` columns gone, `enforce_eval_run_n_observations` back to 004's body, and every
  restored comment **byte-identical** to the 001/004/006 originals (I diffed each against its source
  migration). `close`/`volume`/`announced_at` correctly go back to NULL — I confirmed 002 and 005 never
  commented them. Re-`up` applies cleanly, checksum `ok`.
- **001–006 behave as their tests assert.** `test_real_migrations_up_down_up` now exercises all seven
  through a full cycle and asserts `count(*) FROM schema_migrations == 7`; the 004 evaluation tests
  were extended (not weakened) — every pre-existing `pytest.raises` is retained, with the new NOT NULL
  columns supplied so each insert's *only* omission is still the thing under test. The added
  `_seed_calendar` helper is necessary, not a loosening.
- **The re-adjust corrupted nothing.** 12,840,439 bars (unchanged), 46,934 actions, 2,557 calendar
  days. Spot-check against yfinance over 1,241 paired sessions each — level mean bps / return-diff sd:
  NVDA −0.024 / 6.72 · SPY −0.165 / 4.48 · AGZD −1.659 / 21.28 · AAPL +0.220 / 6.00 · AMZN +0.024 /
  14.82 · GOOGL +0.091 / 4.84 · WMT +0.134 / 3.65 · TSLA −0.534 / 18.51. `ours_only = 0` dates for all
  eight (no spurious sessions); `theirs_only = 20` in every case = 4 pre-archive days + the 15
  December-2024 hole + 2025-10-03. Both NVDA splits correct (2021-07-19 factor 40 → 2021-07-20 factor
  10, adj 18.7798 → 18.6060; 2024-06-07 factor 10 → 2024-06-10 factor 1). **AGZD's 2023-08-10 2-for-1
  handled exactly**: raw 44.2799 → 22.12, factor 2 → 1, `adj_close` continuous 22.1400 → 22.1200,
  matching yfinance 22.14 → 22.20 within the 15:59-close noise. SPY unchanged.

### Gates

`bash bin/local_test.sh` — my own run, exit 0:

```
  PASS   HARD  ruff          (All checks passed!)
  PASS   HARD  screen        (18 passed)
  PASS   HARD  backend       (69 passed)
  PASS   HARD  shellcheck
  PASS   HARD  database      (148 passed in 50.34s)
  PASS   HARD  frontend      (next build ok)

✓ all hard gates passed
```

**148 database tests confirmed.** The report's claim is accurate.

---

## The 60–120 day splice cohort (the fix-pass asked for this specifically)

The fix-pass extended its own threshold from 180 → 120 days mid-verification on live data and asked
for a sanity check on the cohort it did not touch. Here it is, and **the cohort is not benign**.

**Remaining internal holes after the live splice** (full-table, all 12.8M bars):

| Gap length | Gaps | Securities |
|---|---|---|
| 31–59 d | 2,635 | 1,245 |
| 60–89 d | 495 | 362 |
| 90–119 d | 177 | 157 |
| exactly 120 d | 7 | 7 |
| **>120 d** | **0** | **0** |
| **60–120 d total** | **679** | **448** |

The threshold held exactly where it was set — 0 holes above 120 days is the splice working as
designed. **679 gaps across 448 securities sit in the 60–120 day band.** Of those, 31 produce a
≥100% single-"session" move, 23 a ≥200% move, and 6 a ≥900% move; only **5 of the 28 extreme (≥3×)
cases have any split recorded inside the gap** that could explain them.

**I checked the extremes against yfinance. Five out of five are identity breaks, not trading halts:**

| Symbol | Gap | Before → after | Fabricated move | Independent evidence |
|---|---|---|---|---|
| **COHR** | 70 d (2022-06-30 → 2022-09-08) | \$266.29 → \$43.19 | **−83.8%** | yfinance's COHR closes \$50.95 on 2022-06-30 — that is **II-VI**, not old Coherent Inc. Our pre-gap \$266 series is the acquired Coherent Inc; the post-gap series is II-VI renamed. **And II-VI's four splits (1995, 2000, 2005, 2011) are attached to the combined `security_id`** — the exact "22 of the 283" wrong-adjustment bug, unrepaired. Harmless numerically *today* only because all four predate the archive; on the planned 30-year FMP history it would not be. |
| **DBD** | 80 d (2023-05-26 → 2023-08-14) | \$0.2498 → \$20.57 | **+8,134%** | yfinance history begins **2023-08-14**, exactly our post-gap date. Post-Chapter-11 reorganisation equity — old shares cancelled. |
| **VRM** | 83 d (2024-11-29 → 2025-02-20) | \$5.01 → \$31.00 | **+519%** | yfinance history begins **2025-02-20**, exactly our post-gap date. |
| **FNGU** | 116 d (2025-02-28 → 2025-06-24) | \$523.40 → \$22.17 | **−95.8%** | yfinance history begins **2025-02-20**; no splits on record. |
| **FIG** | 69 d (2025-05-23 → 2025-07-31) | \$23.745 → \$116.30 | **+390%** | yfinance history begins **2025-07-31**, exactly our post-gap date — Figma's IPO. Pre-gap bars are Figure Acquisition Corp. |

Also visible and unexplained: a **12-ticker family** (`LIAE LIAQ LIAT LIAY LIAU LIAV LFAQ LFAE LFAW
LFAR LFAO LFAK`) with 62–116 day gaps and a suspiciously uniform 9.4×–10.2× jump, only 2 of which
have a split in the gap. That signature — a dozen sibling tickers, one ratio — is a family-wide
reverse split that **gap-based candidate selection structurally cannot see**, because
`candidates_from_gaps` compares consecutive bars and the split fell inside a multi-month hole. Worth
naming as its own blind spot.

**Is 120 defensible or arbitrary?** It is *empirically motivated* (META at 132 days forced it) but
**arbitrary as a boundary**, and the data says so: the ratio distribution does not thin out below 120
days. 23 securities in 60–120 d and 17 more in 31–59 d carry ≥200% gap moves. The fix-pass's own
argument for splicing generously — "computing a 'return' across a void is exactly the fabricated
number this exists to kill", and splitting a genuinely-halted continuous issuer costs a backtest
nothing — applies with **equal force at 60 days**, so the threshold is not where the reasoning points.

**The stated mitigation does not mitigate.** `FOLLOW_UPS.md`: "Shorter recycles remain possible; the
alignment check is the tripwire." `verify_daily_series.REFERENCE_SYMBOLS` is 20 mega-caps (SPY QQQ IWM
AAPL MSFT NVDA AMZN GOOGL META TSLA JPM XOM KO JNJ PG WMT UNH HD V MA). Ticker recycling happens in
delisted small caps and post-bankruptcy reorgs — a cohort with **zero** overlap with that list. META
was caught by luck of membership, not by design. **No automated check in this repo can see COHR, FIG,
DBD, VRM or FNGU**, and `_series` additionally filters `delisted_at IS NULL`, so pre-gap identities
are invisible to it by construction.

**Recommended (not applied — I do not fix):** a *universe-wide* gap tripwire, not a 20-symbol one —
e.g. a `verify_daily_series` check that fails when any security carries an internal hole above a
configured floor with a cross-gap ratio outside a configured band, which is one query over the same
window function `find_latest_gaps` already uses. Then the threshold becomes tunable-and-observable per
Bar §7.2 rather than a constant chosen against one example.

---

## Untrue-claim audit

Every assertive claim the fix-pass wrote, tested where testable. **REFUTED first.**

| Location | Claim | Verdict | Evidence |
|---|---|---|---|
| `db/verify_daily_series.py:12-13` | check 1 — "A one-session offset fails immediately **and cannot pass by luck**" | **REFUTED** | `check_alignment` derives `lo, hi` from **the symbol's own bars** (`:127-128`), so `expected` slides with the data. A whole-pipeline shift of 1 (or 20) sessions yields `missing=0, extra=0` → PASS. This is *precisely* the failure mode the module's own WHY paragraph cites ("a consistent ONE-session misalignment still lands on 97.0%"). The review asked for the date set over **the window**; the implementation windows to each symbol. |
| `db/verify_daily_series.py:14-16`, log line `:182-183`; `FIX_REPORT_phaseA.md:120` | check 5 — "**every** stored `split_adj_factor`" / "every one of 12.8M stored factors" | **REFUTED (overstated)** | The query is restricted to `security_id IN (… action_type='split')`: **1,648,849 of 12,840,439 bars (12.8%) are in scope; 11,191,590 are never compared.** The property happens to hold — I verified independently that 0 non-split bars carry factor ≠ 1 — but the check cannot see them and the number quoted is wrong. Same defect in `load_corporate_actions.py:583-600`. |
| `db/verify_daily_series.py:31-34`, `:62-63` | provider bounds are "pinned to the current basis"; the symbol set "deliberately includes split cases (NVDA, AMZN, GOOGL, TSLA)" | **REFUTED — and this is a blocker** | **Ran it.** `--provider` on the default set → `check 2: NVDA: close vs official worst 390813.8 bps (bound 100.0), mean +115262.338 bps (bound ±1.0)`, **exit 1**. Line 214 compares our **raw** `close` to yfinance's **split-adjusted** `Close`, discarding the `adj_close` it fetched. SPY/QQQ/IWM/AAPL/MSFT pass only because none has an in-window split. See B-N1. |
| `db/verify_daily_series.py:62` | "**Continuously-listed** … reference names" | **REFUTED** | META is not: two identities post-splice; the live one has 817 bars, not 1,241. Check 1 still passes only because `expected` is windowed to the symbol's own first bar. |
| `FIX_REPORT_phaseA.md:52` (B-S3) | "The documented formula no longer mixes bases **anywhere**" | **REFUTED** | `004_evaluation.up.sql:484` and `005_corporate_actions.up.sql:18` still state `Σ shares × adj_close + cash`. See SF-1. |
| `FOLLOW_UPS.md` (sub-120-day recycles) | "the alignment check is the tripwire" | **REFUTED** | 20 hardcoded mega-caps; `_series` filters `delisted_at IS NULL`. Cannot see any member of the 448-security cohort. |
| `007_point_in_time.up.sql:16` | "post-decision information into **308,709 bars**" | **REFUTED (stale)** | Live figure is **304,870**. Drifted after the re-fetch/splice. Minor in size, but it is a wrong number inside an applied, checksummed migration comment — the exact defect class. |
| `db/load_daily_bars.py:8` | "Deriving daily … is **~11 million rows**" | **REFUTED** | 12,840,439 (12.8M) — as `006:31` and `load_reference_data.py:20` both correctly state. ~17% understated. |
| `db/load_daily_bars.py:136` | "All 15 corrupt members observed so far raise **the first three**" of the tuple | **REFUTED** | Measured: 14 × `gzip.BadGzipFile` (an `OSError`) + 1 × `zlib.error`. **`EOFError` is raised by none of them.** The tuple is right to be wide; the claim about which types fire is wrong. |
| `db/load_reference_data.py:32-34` | DGS3MO − DTB3 spread is "+0.21 pp in the 5%+ regime … **all one direction**" | **REFUTED** | n=1,250 paired: mean +0.1177 pp ✓, max +0.2800 pp ✓ — but **35 sessions are negative** (min −0.020) and 348 are exactly 0. 5%+ regime is +0.2265, not +0.21. |
| `db/load_reference_data.py:98-99` | "**capped** exponential backoff with jitter" | **REFUTED** | No cap exists (`:299`, `2.0 * 2**(attempt-1) * (0.5+random())`); growth is bounded only by `FETCH_ATTEMPTS = 4`. The companion "~15 s total" claim *is* right (∈[7,21) s). |
| `db/load_minute_bars.py:22-23` | 2024-12-10 "inflates for **1,266,148** rows" | **REFUTED (off by one)** | 1,266,147 data rows; 1,266,148 counts the header. `stats.rows_read` counts data rows. |
| `db/migrations/006:8` (pre-existing, not the fix-pass) | "NUWE … cumulative factor **≈1e-10**" | **REFUTED** | NUWE's full product is 7.71e-13 — 130× smaller. The comment is internally inconsistent: its own "\$21 trillion" figure is only reachable *from* 7.71e-13. Repeated at `load_corporate_actions.py:463`. |
| `docs/DATA_INVENTORY.md:117-118` | "the split adjustment is **verified exact against an independent provider**" | **CONFIRMED as a data claim, REFUTED as a tooling claim** | The data claim is true — I reproduced it today from scratch (NVDA mean −0.024 bps over 1,241 sessions vs yfinance). But **nothing in the repo does this**: check 5 recomputes with the same in-repo `split_factor_between` that wrote the factors (a staleness test, not an independence test), and the one provider-facing check compares the wrong column and fails. The sentence reads as a standing property when it is a one-off manual finding. |

**CONFIRMED** (tested, not assumed):

| Location | Claim | Evidence |
|---|---|---|
| `load_daily_bars.py:34-38` | 6.8% of 1,241 SPY closes match to half a cent; open matches to the cent | 85/1,241 = 6.849%; opens 1,240/1,241 within 1¢. |
| `load_daily_bars.py:31-42` | auction is the 16:00 bucket's HIGH on SPY 2025-04-09, its OPEN on MSFT 2023-12-15; bucket close 544.30 vs official 548.62 | Read from the archive; 543.37/548.62 − 1 = −95.69 bps = the stated worst case. |
| `load_daily_bars.py:36, 49-50` | volume ~15% low, median 0.853 | Live provider run: 0.853. |
| `load_daily_bars.py:45-47` | half-days capture the auction (nothing 13:01–15:59) | 2024-11-29 and 2023-07-03: SPY and AAPL each 211 in-window bars, 0 in (13:01, 15:59). |
| `load_daily_bars.py:57-60`; `007` comment on `split_adj_factor` | "adjust exits non-zero while any factor is NULL" | `load_corporate_actions.py:555-557` returns `EXIT_VALIDATION`. Live: 0 NULL factors, 0 NULL `adj_close`. |
| `load_daily_bars.py:150-156`, `408-419` | all four OHLC reach COPY as source strings; exact-`Decimal` pre-screen; ≤390-bit bucket bitmask | Verified by reading the COPY binding and the `DayBar` field order. |
| `load_minute_bars.py:21-26, 282-284` | 15 corrupt members, 14 at header + 1 mid-inflate; mid-COPY corruption rolls back cleanly and the run continues | Reproduced live over the real 2024/12 directory: 6 corrupt reported, 6 healthy files committed, exit 1. psycopg 3.3.4's `LibpqWriter.finish` preserves the original exception, so `CorruptArchive` propagates out of the transaction unclobbered. |
| `load_reference_data.py:9-11, 37-49, 52-53, 101, 174-177` | 15-session hole; `known_at = eff+1d` conservative vs H.15 ~16:15 ET; ±1 CHECK; `Decimal(raw)/100`; 4 attempts → exit 3; 2021-12-31 open with 10,871 bars | All verified against the live DB and 004's DDL. Revisions **are** representable: PK is `(series, effective_date, known_at)`. |
| `007` header + comments | as-of asymmetry (returns safe at every t, levels only at/after as-of); `announced_at` reasoning; rf-conversion formulas; coverage machinery | Each restated claim checks out against the DDL, the trigger definition, and the data. |
| `DATA_INVENTORY.md:123-137` | dividends ~5%, SPY zero; `first_seen` censored for ~8,700; metadata unpopulated | 960/19,713 = 4.87%; SPY 0; 8,678 at 2020-10-02; all five metadata columns 100% NULL. |

**The signature defect is materially reduced but not eliminated.** The four *load-bearing* mislabels
the reviews found — `adj_close` holding post-decision information, `total_return` able to hold a price
return, a marking formula on two share bases, a close claiming to be the official close — are each
closed at the point of damage. What slipped through are **overclaims about the verification itself**
("cannot pass by luck", "every one of 12.8M", "the tripwire", "no longer mixes bases anywhere"), which
is a different and in some ways more dangerous flavour: a claim that a guard exists when it does not.

---

## New findings

### BLOCKER

- **B-N1 — `verify_daily_series.py --provider` compares raw closes against split-adjusted closes,
  and fails on a correct database.** `db/verify_daily_series.py:214`:

  ```python
  paired = [(d, c, theirs[d][0], v, theirs[d][1]) for d, c, _a, v in ours if d in theirs]
  ```

  `_series` (`:95-102`) returns `(trade_date, close, adj_close, volume)`. `c` binds the **raw**
  `close`; `_a` — the `adj_close` that was just fetched — is discarded. `theirs[d][0]` is yfinance's
  `Close` at `auto_adjust=False`, which Yahoo returns on the **current split basis**. Observed live:

  ```
  provider checks PASS SPY:  close worst 95.7 bps / mean -0.165 bps, ret-sd 4.48 bps, corr 0.99925, vol -1.43%, volume median 0.853
  provider checks PASS MSFT: close worst 73.9 bps / mean +0.055 bps, ret-sd 6.13 bps, corr 0.99931, vol +0.07%, volume median 0.772
  ERROR check 2: NVDA: close vs official worst 390813.8 bps (bound 100.0), mean +115262.338 bps (bound ±1.0)
  ERROR 1 verification check(s) FAILED                                            → exit 1
  ```

  Three consequences, in order of severity. (1) **Checks 3 and 4 are dead for every in-window split
  name** — the paired-return-sd and realized-vol tests the review called "what a level bound alone
  lets through" and "the denominator of every ratio", disabled for exactly the cohort where an
  adjustment error can live. AMZN, GOOGL, TSLA and WMT will all fail the same way; AAPL passes only
  because its 2020-08-31 split predates the archive. (2) It **red-lights a correct database**, which
  trains an operator to ignore it or to widen `CLOSE_MAX_ABS_BPS` — the worst available outcome. (3)
  The provider loop raises on the first failure rather than collecting, so symbols after NVDA are
  never checked at all. `FIX_REPORT_phaseA.md:60`'s S-S6 "FIXED" row cannot have been executed.
  Bar §0 [P0] fail-loud is satisfied; §5.3 test-quality is not — this check cannot fail for the
  reason it exists. Fix is one token: bind `_a` (and use `NULLIF`/skip where `adj_close IS NULL`).

- **B-N2 — 448 securities still splice two issuers into one price series, and no check can see
  them.** Detail and independent provider confirmation in the cohort section above: 679 internal
  holes in 60–120 days, 23 securities with a ≥200% fabricated single-"session" move, 5 of 5 sampled
  extremes confirmed as identity breaks against yfinance (COHR, DBD, VRM, FNGU, FIG), plus COHR
  carrying II-VI's split history on the acquired company's prices. This is the same defect B-S5 was
  raised for, at a shorter gap length, and the documented tripwire is a 20-mega-cap list that has
  zero overlap with the affected cohort. Bar §7.2 [P1] "no survivorship bias (include delisted)" and
  `EVALUATION_FRAMEWORK` §3.5. Rated BLOCKER not SHOULD-FIX because a −83.8% or +8,134% "daily
  return" will dominate any max-drawdown, hit-rate or tail statistic computed over this universe, and
  it is indistinguishable from a real event without external data.

### SHOULD-FIX

- **SF-1 — the mixed-basis marking formula survives in two migration files, and the fix report says
  it does not.** `db/migrations/004_evaluation.up.sql:481-484` — the reasoning block a marking-job
  author will actually read — still ends `-- Σ shares x adj_close + cash.`, and
  `db/migrations/005_corporate_actions.up.sql:18` still asserts `this system marks portfolios as
  \`Σ shares × adj_close + cash\`` as the premise of 005's dividend argument (the conclusion survives
  the premise change, but the premise is now false). These are `--` comments, not `COMMENT ON`, so the
  checksum constraint that justified routing the catalog fix through 007 does not prevent 007 from at
  least citing them by file:line, nor `PROJECT_PLAN.md` from doing so (it already carries the
  correction). Remove "anywhere" from `FIX_REPORT_phaseA.md:52`.

- **SF-2 — the B-S1 residual is larger at a mid-window decision date than the report's framing
  conveys, and is guarded only by a comment.** The fix-pass correctly flagged that stored levels stay
  unsafe for a decision at historical time *T*. I quantified it at T = 2023-06-30 (a plausible
  walk-forward boundary), comparing stored `adj_close` against
  `close ÷ split_factor_between(security_id, trade_date, T)`:

  | Measure (bars with trade_date ≤ T and a later split) | Count |
  |---|---|
  | bars in scope | 529,937 |
  | stored level differs from the PIT-correct level | 376,645 bars / 683 securities |
  | **wrongly PASS a \$5 floor on the stored column** | **148,909 bars / 469 securities** |
  | wrongly FAIL a \$5 floor on the stored column | 2,068 bars / 12 securities |

  For comparison, the original B-S1 demonstration was 196,909 bars / 357 securities. So at a
  mid-window date the *screening* exposure is of the same order as the bug that was called a blocker
  — the fix moved the contamination from "unconditional" to "conditional on the decision date being
  before archive end", which is the correct and honest outcome, but §3.5 asks for structural
  enforcement and this is a `COMMENT ON` saying "MUST NOT". The good news: **the mitigation is
  genuinely usable** — a whole-universe single-session \$5 screen through
  `split_factor_between` runs in **207 ms** (7,844 rows at 2023-06-30), and a one-year single-symbol
  lookback in 5.7 ms. So the cost of making it structural is low. Recommend a
  `pit_adjusted_close(security_id, trade_date, as_of)` SQL function or a decision-date-parameterised
  view as the *only* sanctioned level path, plus a lookahead test per `PROJECT_PLAN` Phase 4 that
  fails if a feature query references `adj_close` directly.

- **SF-3 — gap-based candidate selection structurally cannot find a split that occurred inside a
  multi-month hole.** `candidates_from_gaps` compares consecutive bars, so the 12-ticker `LIA*`/`LFA*`
  family (uniform 9.4×–10.2× across 62–116 day holes, only 2 with a recorded split) is invisible to
  it. This is a distinct blind spot from the documented "small actions fall below the threshold"
  caveat and is not mentioned anywhere. One extra selector — securities with a large ratio across a
  hole of any length — closes it and reuses `find_latest_gaps`.

- **SF-4 — `adjustment_as_of` is derived, not declared.** `cmd_adjust` sets it from
  `max(trade_date) FROM price_bars_daily` (`load_corporate_actions.py:427-431`), while 007's header
  and the table comment both call it "DECLARED". It is recorded and auditable, so the substance is
  fine, but an operator cannot pin the adjustment to an earlier as-of (e.g. to reproduce a historical
  walk-forward fold) without editing code — and `EVALUATION_FRAMEWORK` §3.5's walk-forward
  requirement will want exactly that. Add `--as-of` with the derived value as the default, and either
  soften the wording or make it true.

- **SF-5 — check 1's excused-gap set is derived from the data it checks.** `check_alignment`
  computes `global_gaps` as "calendar sessions where the whole archive has no bars" (`:110-119`). The
  per-symbol comparison is still meaningful (a symbol missing a session the universe has is not
  excused, which is how META was caught), and the count is logged — but a *universe-wide* coverage
  loss can never fail this check, which is why the December-2024 hole passes silently. Pair it with an
  assertion on the expected gap count (15 today) so a 16th hole is loud.

### NIT

1. `verify_daily_series.py:75` — `VOLUME_MEDIAN_BAND = (0.75, 0.98)` is documented from SPY's 0.853,
   but MSFT measures **0.772**, i.e. 0.022 from the floor. `RETURN_CORR_MIN = 0.999` against SPY's
   actual 0.99925 leaves 0.00025 of headroom; the review asked for ≥0.9999, which SPY would fail. The
   thresholds are honestly pinned to the current basis, but nothing says how close to the edge they
   already sit.
2. `load_reference_data.py:32-34` — "all one direction" (see audit table); 35 negative sessions.
3. `load_reference_data.py:279` — logs `len(AD_HOC_CLOSURES)` as "applied" without filtering to
   `[--from, --to]`.
4. `load_minute_bars.py:518` — a `--dry-run` summary prints "N file(s) loaded … 0 rows written";
   nothing was loaded.
5. `load_reference_data.py:122-124` — the ad-hoc-closure list names 9/11 and Sandy but omits
   **2018-12-05** (Bush mourning), 2007-01-02 (Ford) and 2004-06-11 (Reagan). A range moved back to
   2018 is far likelier than to 2001, and would mark 2018-12-05 a trading day — the precise failure
   the block warns about.
6. `load_reference_data.py:98-99` — "capped" backoff has no cap.
7. `load_daily_bars.py:108-109` — `NS_MIN`/`NS_MAX` are defined and never used in this module
   (Bar §0 [P0] clean tree).
8. `006:38` — "NUMERIC(30,12) … covering every ratio observed and a wide margin": the *unbounded*
   products actually observed (NUWE 7.71e-13) fall below the column's granularity; anything <5e-13
   would round to 0 and violate `ck_price_bars_daily_factor`. Safe today **only** because 007's as-of
   bound holds the minimum stored factor at 3.3e-8 — which is worth stating, because it is a real
   dependency of 006 on 007.

### PRAISE

- **B-S1's design is better than either option the review offered, and it survived an
  independent-method audit.** Dropping `split_factor_after` outright rather than deprecating it turns
  "don't do this" into "you cannot", which is what §3.5 actually asks for. Recording the bound in a
  CHECK-enforced singleton written *before* the factors means a factor column can never exist without
  its as-of. And the asymmetry insight — that the factor *ratio* makes returns PIT-safe at every t
  while only levels are poisoned — is carried correctly into the catalog comment, the docstring, and
  the two cross-checks. My exact-numeric recomputation over 1,648,849 bars found zero disagreement to
  twelve decimal places. That is the strongest single piece of work in this fix-pass.
- **The B-S4 revert-proofing caught its own bug and the fix report says so.** A pin whose first draft
  derived its anchors from the constant under test would have passed under the forbidden change — a
  self-referential test, the same defect class as this project's untrue comments. Finding that during
  revert-proofing, re-anchoring to absolute ET wall-clock, and then *documenting the near-miss in the
  report* is exactly the behaviour a review cannot enforce. I re-ran it and it is genuinely red.
- **Three rejections are argued better than the review's own recommendations.** S-S8's `NOT NULL`
  refusal correctly identifies that a `DEFAULT 1` would bake in the very lie the finding warns about.
  N-9's reversal correctly spots that "fetch delisted identities too" is the B-S5 mis-attribution
  wearing a different hat. S-S4's decision to keep `effective_date + 1d` as a floor for first
  observations correctly spots that a fetch-time `known_at` on a 45-year backfill would hide every
  historical rate from every historical decision. A fix-pass that pushes back with reasoning, and
  records the reasoning where the code lives, is doing the job properly.
- **B-1 and B-2 were fixed against reality, not against a fixture.** I reproduced both the way an
  operator would hit them — a genuinely internal Docker network, and the genuinely corrupt
  2024-12-10 member — and the behaviour matched the report line for line, including the 15-call
  short-circuit budget and the "no egress? wrong wrapper?" diagnosis.
- **The live repair was sequenced correctly and the counts reconcile exactly.** 19,301 + 289 + 123 =
  19,713 securities; 7,621 + 289 + 123 = 8,033 delisted; 18,133 + 11,225 = 29,358 rates; 12,840,439
  bars unchanged. Running `adjust` only *after* the as-of bound landed — per the review's explicit
  warning — is the difference between this fix and re-baking lookahead into 12.8M rows.
- **The known-residuals list is honest, specific, and mostly right.** Naming the December hole, the
  5% dividend coverage, the unfetchable delisted actions, and the sub-120-day recycles rather than
  burying them is why this review could be targeted rather than exploratory. The one thing wrong with
  it is the claimed tripwire, not the disclosure.

---

## Recommendation: one more targeted fix-pass, then ship Phase A

Not a re-do. Three items, all small, none touching the 12.8M-row adjustment that is now verifiably
correct:

1. **B-N1** — bind `_a` in `verify_daily_series.py:214` and collect provider failures instead of
   raising on the first. Then run `--provider` green over all 20 symbols and record the actual
   per-symbol numbers, because the current thresholds have never been exercised against a split name.
   Add a regression test that fails if a raw column is compared to an adjusted one.
2. **B-N2 / SF-3** — decide the splice threshold on the ratio distribution rather than on META, and
   replace the 20-symbol "tripwire" with a universe-wide gap check that is tunable, observable, and
   fails loudly (Bar §7.2). At minimum, splice or explicitly quarantine the 23 securities carrying
   ≥200% cross-gap moves in 60–120 days, and re-attribute COHR's II-VI splits.
3. **SF-1, SF-2, SF-4** and the audit's stale numbers (`308,709 → 304,870`, `~11 million → 12.8M`,
   the `EOFError` claim, "all one direction", "capped", "cannot pass by luck", "every one of 12.8M",
   "no longer mixes bases anywhere") — a documentation-truth pass. Cheap, and this project's history
   says it is not optional.

**Do not gate Phase B's schema on this.** Migration 007 is right, its down migration reverses
cleanly, all 148 database tests pass, and nothing consumes the data yet — which is the window in
which every one of these is cheap and after which, per §3.5, some of them stop being fixable.

---

**Verification hygiene.** All live-database access was `SELECT` / `COPY … TO STDOUT` (plus session-local
`SET statement_timeout = 0` and a `pg_temp` aggregate); no INSERT/UPDATE/DELETE/DDL against any
protected table on `rh-db`. Every write test ran against a throwaway `postgres:16-alpine` on a scratch
`--internal` network, both removed afterwards. Loader reverts were applied to a copy of `db/` under
the scratchpad and each file restored `diff`-confirmed byte-identical. `data/market/` was mounted
`:ro` in every container and is unmodified. No `docker prune`; no `km-*` container, volume or network
touched — all verified `Up (healthy)` afterwards, as are `rh-db` and the `uvrl-*` stack. `rh-db`:
`running healthy`, migrations 001–007 `applied` / checksum `ok`, `price_bars_daily` 12,840,439 rows.
`git status` matches arrival exactly, plus this file.
