# Fix report — Phase A loaders + semantics reviews

**Fix-pass engineer:** independent (did not write, did not review). **Date:** 2026-07-29.
**Inputs:** `REVIEW_phaseA_loaders.md` (2 BLOCKER / 7 SHOULD-FIX / 9 NIT),
`REVIEW_phaseA_semantics.md` (5 BLOCKER / 9 SHOULD-FIX / 3 NIT), `SENIOR_ENGINEER_BAR.md`
(§0, §1, §4, §7.2), `EVALUATION_FRAMEWORK.md` §2/§3.5, migrations 005/006.

**Shape of the fix:** migration `007_point_in_time` (+ destructive down) carries every schema
change and every comment correction — 005/006 are applied and checksummed, so nothing edits an
applied file. Loader changes land in the four existing loaders plus two new tools
(`db/load_delistings.py`, `db/verify_daily_series.py`). 31 new tests
(`db/tests/test_loader_units.py`, `db/tests/test_loaders_db.py`); every BLOCKER has a regression
test proven red-on-revert (§Revert evidence). The live database was then actually repaired:
007 applied, 289 spliced identities, 7,621 delistings marked, factors re-adjusted under the
as-of bound, DGS3MO loaded.

**Pre-existing red found on arrival:** `test_real_migrations_are_classified_from_filenames`
pinned the classification list at 001–004 while 005/006 were already in the tree — the database
suite was failing before this fix-pass touched anything. The pin now covers 001–007.

---

## Dispositions — loaders review

| ID | Sev | Disposition | Note |
|---|---|---|---|
| B-1 | BLOCKER | **FIXED** | `fetch_actions` now raises typed `ProviderError` (guarded yfinance import included); `cmd_fetch` counts failures, WARNs per symbol, lists them in the summary, exits `EXIT_VALIDATION` when any occurred, and short-circuits to `EXIT_CONNECTION` when the first 15 calls all fail (the no-egress/wrong-wrapper case dies in seconds, not hours). "Could not ask" and "no actions" are now distinguishable end to end. |
| B-2 | BLOCKER | **FIXED** | Full corrupt-archive machinery ported to `load_minute_bars.py` (`CorruptArchive`, guarded open/header, `_rows_or_corrupt` around iteration, collect-report-continue loop, final `EXIT_VALIDATION`), with the S-1 widened tuple from day one. A mid-COPY failure still rolls the file back inside its transaction — the propagation path is documented as load-bearing. |
| S-1 | SHOULD-FIX | **FIXED** | Shared `CORRUPT_STREAM_ERRORS = (OSError, EOFError, zlib.error, UnicodeDecodeError, csv.Error)` in both bar loaders, applied at all three catch sites; `sha256_of` OSError also folded into the report-skip-continue channel in both. |
| S-2 | SHOULD-FIX | **FIXED** | `DayBar` carries `high_s`/`low_s` source strings alongside the comparison floats; COPY writes the strings — the money path is now string→NUMERIC end to end for all four OHLC fields (P-2's standard). The related float pre-screen was tightened to exact `Decimal` comparison so the screen can no longer be weaker than the CHECK it fronts. |
| S-3 | SHOULD-FIX | **FIXED** | `fetch_fred_csv`: 4 attempts, capped exponential backoff + jitter, injectable sleep; terminal failure raises `FetchError` → `EXIT_CONNECTION` (3), no longer `EXIT_VALIDATION` (1). |
| S-4 | SHOULD-FIX | **FIXED** | `data_sources` row and rate rows now commit in ONE transaction; `row_count` records rows actually inserted (new + revisions), not rows fetched. |
| S-5 | SHOULD-FIX | **FIXED** | `rowcount == 0` conflicts are re-read and value-compared; a differing stored value WARNs with both values and is counted in the summary (`stale-conflict warnings`). Stored value deliberately not overwritten — actions are provenance-bearing history; the disagreement is surfaced, not silently resolved. Regression-tested (`test_s5_conflicting_action_value_warns`). |
| S-6 | SHOULD-FIX | **FIXED per the B-S4 adjudication** | Routed to the semantics reviewer, who ruled: half-days are fine (better than full days — they capture the auction), full days are B-S4, and the fix is documentation + a future official-close source, explicitly NOT moving `SESSION_LAST_MINUTE`. Implemented exactly that; see B-S4 below. |
| S-7 | SHOULD-FIX | **FIXED** | 22 unit tests (`test_loader_units.py`: calendar rules incl. the New-Year's-Saturday exemption and the Carter closure, DST session bounds, DayBar fold, corrupt-stream classification across all five exception types, FRED parse/retry, provider-error contract, argparse) + 9 testcontainers tests (`test_loaders_db.py`). db suite: 117 → 148. |
| N-1 | NIT | **FIXED** | `parse_fred_csv` returns `Decimal(raw)/100`; correctness no longer depends on column scale. |
| N-2 | NIT | **FIXED** | `_open_csv(path) -> tuple[io.TextIOBase, Iterator[list[str]]]`. |
| N-3 | NIT | **FIXED** | `--limit` uses a `positive_int` argparse type in both bar loaders; `--limit 0` is a usage error, not "no limit". |
| N-4 | NIT | **FIXED** | `psycopg.OperationalError → EXIT_CONNECTION` in all four loaders (and the two new tools), caught before the general `psycopg.Error → EXIT_SQL`. |
| N-5 | NIT | **FIXED** | `import yfinance` guarded; a missing module is one clear `ProviderError` line naming the correct wrapper, not a traceback. |
| N-6 | NIT | **FIXED (with S-S9)** | The misleading "~1e13" comment is gone. The guard itself was also wrong the other way (see S-S9): replaced by `MAX_MEANINGFUL_ADJ_CLOSE = 1e12` with a comment stating NUMERIC(30,10) holds < 1e20 and the cutoff is about *meaning*, not representability. |
| N-7 | NIT | **FIXED** | Skipped MINUTE rows and dropped DAY bars are separate counters, separately reported. |
| N-8 | NIT | **FIXED** | Per-`DayBar` minute-bucket bitmask (≤390 bits/symbol — near-free where per-key sets would cost hundreds of MB); a duplicate `window_start` is skipped and counted, never folded in twice. Unit-tested. |
| N-9 | NIT | **FIXED, opposite direction from the review's lean — with rationale** | The review leaned "all is arguably the correct one (delisted securities still have bars needing adjustment)". True for *adjustment*, but wrong for *fetching*: yfinance resolves a symbol to its CURRENT holder, so fetching by symbol for a delisted identity attributes the re-listed issuer's actions to the dead one's series — the exact B-S5 bug. All three selectors now resolve to live holders only, and the excluded-delisted count is loudly reported with this rationale in the code. Dead identities' actions need an identity-aware feed; that limitation is documented in DATA_INVENTORY. |

## Dispositions — semantics review

| ID | Sev | Disposition | Note |
|---|---|---|---|
| B-S1 | BLOCKER | **FIXED — schema, code, and data** | 007 adds `split_factor_between(security_id, bar_date, as_of)` (bound: `ex_date > bar_date AND ex_date <= as_of`) and **DROPS `split_factor_after`** — recomputing a factor without stating an as-of is now impossible, not discouraged. The materialised columns are pinned to a declared `adjustment_as_of` recorded in the new singleton `price_adjustment_state` (written *before* factors). `adjust` was re-run live: 527 post-archive splits excluded (logged), PAVS 2025-09-30 back to $1.04, $5-floor contamination 0 (§Live verification). The asymmetry drove the design: stored factors make RETURNS point-in-time safe at every t (factor(t−1)/factor(t) embeds only splits with ex_date ≤ t); LEVELS at a historical decision date T must use `split_factor_between(…, T)` — the catalog comment on `adj_close` says exactly this and `verify`/`verify_daily_series` cross-check every stored factor against the bounded recomputation. **announced_at contradiction resolved by reasoning, not deletion:** a split is market-observable ON its ex-date, so ex-date-bounded adjustment uses only events that had already publicly happened — no announcement timestamp needed; `announced_at` matters only for pre-ex-date anticipation, which nothing does. 005's rule stands for that use; 007's comment on the column states this. |
| B-S2 | BLOCKER | **FIXED (schema + honesty) / DEFERRED (data completion)** | The schema can no longer call a price-only number a total return: `evaluation_runs.return_basis TEXT NOT NULL CHECK IN ('price_only','total_return')`, no default — every writer states which number it computed. The loader docstring now carries the measured coverage (5.0%, SPY zero) and the structural reason gap selection cannot find dividends. The full-universe dividend fetch itself is DEFERRED as an operational run: `--candidates all` is ~14,600 rate-limited yfinance calls (hours), and per the review's own fix ordering it must run *after* B-1 (done) so failures are loud — it is now safe to run and is the documented next step. Until it lands, `total_return` cannot silently hold a price return, which is the review's stated minimum. |
| B-S3 | BLOCKER | **FIXED** ~~anywhere~~ *(corrected in round 2: the old formula survives as prose in applied migrations 004:484 and 005:18 — superseded by 007's catalog comments and cross-referenced in 008's header, since applied files cannot be edited)*: the documented marking formula in the CATALOG no longer mixes bases: marking is `Σ shares × RAW close + cash` with lot share counts kept on the as-traded basis (multiply shares / divide entry_price on each split ex-date). Corrected via 007 `COMMENT ON` for `paper_portfolio_positions` (which is where 004's formula lives in the catalog) and `paper_portfolios.cash`; `adj_close`'s comment now ends "NEVER a marking price"; PROJECT_PLAN.md's restatement of the old formula is corrected in place. Regression test asserts the catalog text. (With raw×raw marking, the review's `entry_price NUMERIC(18,6)`-cannot-hold-adj_close concern dissolves — no adjusted level ever enters the marking domain.) |
| B-S4 | BLOCKER | **FIXED (honest documentation + behaviour pin + plan)** | Per the review's explicit warning, `SESSION_LAST_MINUTE` was NOT touched. The false "convention every daily OHLCV feed uses" claim is gone; the loader docstring and 007's catalog comments on `close` and `volume` now state precisely what the series is (15:59 ET bar; auction print unrecoverable from minute aggregates; measured bounds — 6.8% half-cent matches, worst 95.7 bps, mean −0.165 bps; volume ~15% low; open DOES match; half-days DO capture the auction) and what the fix is (an official-close source, e.g. Polygon daily aggregates). `test_bs4_daily_close_is_the_1559_bar_by_design` pins the exclusion with ABSOLUTE wall-clock anchors so the forbidden "move it to 16:00" change fails loudly. `verify_daily_series.py` carries the documented deviation bounds and the tighten-on-real-close plan. ADV/slippage caveat added to DATA_INVENTORY. |
| B-S5 | BLOCKER | **FIXED** | New `db/load_delistings.py`. `splice`: 289 identities split live (283 first pass — the review's exact count — plus recursive passes for multiply-recycled tickers, e.g. RIVr → three identities), pre-gap issuer delisted at the gap, post-gap issuer a new row, ALL corporate actions re-attributed to the current holder (kills the 22 wrong-adjustment splices), per-security transactions, refuses securities with scored-history references. `infer`: 7,621 dead names (the review's exact count) marked `delisted_at = last bar + 1` by absence over the final 5 sessions. **FMP investigated as directed:** the stable `delisted-companies` endpoint works with the existing key BUT the free tier serves only page 0 (100 most recent records; page ≥ 1 → HTTP 402, observed live) — all 99 usable records post-date the archive, so 0 historical matches; the `fmp` command handles the tier boundary (4xx never retried, partial coverage used, loud explanation) and becomes genuinely useful for keeping `delisted_at` current going forward. Historical delisting *dates* therefore remain inference-based, stated in provenance and DATA_INVENTORY. `adjust`'s new stale-factor reset cleans the split identities' factors (verified live: 6,595 + 1,156 bars reset across the two adjust runs). **Threshold extension, decided during verification:** the new alignment check (S-S6) caught META — the Roundhill ETF → Facebook recycle, a 132-day gap at $12.30 → $183.97 (+1,396% fabricated) — *below* the review's 180-day threshold. The 120–180d cohort (123 securities) was examined: its extreme cases are all identity breaks (GPOR post-Chapter-11 $0.14 → $72.95, DXF 328×, SPRB 133×), and splitting a genuinely-halted continuous issuer is the conservative error for a backtest (no return across a void either way). The live splice was therefore re-run at `--min-gap-days 120` (123 further identities; the command's default stays 180 = the review's definition, threshold parameterized). Residual: recycles with gaps under 120 days remain possible and are the alignment check's job to surface. |
| S-S1 | SHOULD-FIX | **FIXED** | `evaluation_runs.expected_sessions INTEGER NOT NULL` — verified by the extended `enforce_eval_run_n_observations` trigger against `market_calendar` (a window with zero calendar coverage is refused outright); `CHECK (n_observations <= expected_sessions)`; generated stored `coverage_ratio`. A 15-session hole is now a visible 0.9x ratio on the row, and a false `expected_sessions` claim is unstorable (tested). |
| S-S2 | SHOULD-FIX | **FIXED** | `rf_conversion TEXT NOT NULL CHECK IN ('simple','compound','annual')`, each formula spelled out in the migration and the column comment (the /252-vs-/365 7% Sharpe swing is cited on the column). |
| S-S3 | SHOULD-FIX | **FIXED** | `FRED_SERIES = "DGS3MO"` (investment basis — the series 004's comment always named), with the discount-vs-investment reasoning and measured spread in the docstring. Loaded live: 11,225 observations 1981→2026. DTB3 rows remain (series is part of the identity); consumers read DGS3MO. |
| S-S4 | SHOULD-FIX | **FIXED** | Revision-aware `known_at`: first observation of a date keeps the `effective_date + 1d` floor (deliberately — a fetch-time known_at on a backfill would hide every historical rate from every historical decision); a value that DIFFERS from the latest stored value inserts a NEW row at fetch time; unchanged values insert nothing. `known_at` is no longer a pure function of `effective_date`, so revisions are representable and re-runs don't bloat. Docstring's "very slightly optimistic" self-criticism replaced with the review's correct "conservative" analysis. |
| S-S5 | SHOULD-FIX | **FIXED** | DATA_INVENTORY re-scoped: "sufficient for a curated-watchlist backtest, not a universe backtest", with the quantified limits (dividend coverage, residual gap cohort, 15:59 close, delisted-identity actions, metadata NULLs) enumerated. |
| S-S6 | SHOULD-FIX | **FIXED** | `db/verify_daily_series.py` implements the review's six-part test: (1) exact per-symbol date alignment vs calendar minus registered gaps — a one-session offset cannot pass; (5) factor PIT cross-check against the as-of bound (also folded into `load_corporate_actions verify`, which now refuses to pass without `price_adjustment_state`) — with the explicit note that yfinance's current-basis series *failing* to match post-archive-split names is the fix working; (2)(3)(4)(6) close-bound / paired-return-sd / realized-vol / volume-band vs provider under `--provider`, thresholds pinned to the documented 15:59 basis with a stated tighten-on-official-close plan. PROJECT_PLAN's restatement of the old endpoint test corrected. |
| S-S7 | SHOULD-FIX | **FIXED (documentation) / DEFERRED (population)** | 007's comment on `securities.first_seen` states the left-censoring concretely (8,678 rows at 2020-10-02 = archive start) and that metadata columns are unpopulated so universe queries cannot yet exclude warrants/rights/preferreds. Populating name/exchange/type needs a reference-metadata feed (FMP profile is per-symbol ≈ 14,600 calls — not free-tier feasible); deferred with the limitation stated in DATA_INVENTORY. |
| S-S8 | SHOULD-FIX | **FIXED by mitigation — NOT NULL rejected with rationale** | A `NOT NULL`/trigger on `split_adj_factor` would either block bar loads (bars must land before `adjust` runs) or require a per-row DEFAULT 1 — which is precisely the silent lie S-S8 warns about, now baked in at load time. The live corruption path the review identified (B-1 silent "no actions" → factor 1) is closed by B-1's fix; `adjust` already exits non-zero while any factor is NULL; `verify` now refuses to pass without the adjustment state; and the factor PIT cross-check catches stale factors. Residual risk — a consumer reading `close` directly — is a consumer-side rule the marking job must enforce; flagged for the marking-job spec. |
| S-S9 | SHOULD-FIX | **FIXED** | The dead `< 1e19` guard replaced by `MAX_MEANINGFUL_ADJ_CLOSE = 1e12`: 006's "NULL means use the factor" contract is now reachable (a level above $1e12/share NULLs and is tested reachable in the suite). Live outcome worth stating precisely: after the B-S1 bounding, the archive's worst adjusted level fell from 2.63e12 (ADTX) to 2.658e8 — the trillion-dollar levels were themselves mostly lookahead artefacts — so the guard currently NULLs 0 live rows. It is a real defence for the 30-year FMP history, no longer dead code against the wrong ceiling. Comment states the true NUMERIC(30,10) ceiling (<1e20) and that the cutoff is semantic, not storage. |
| N-S1 | NIT | **DEFERRED with rationale** | Renaming `adj_close → split_adj_close` is right in principle and cheap in the catalog, but it touches every SQL string in the loaders, tests, verify tooling and docs mid-fix-pass; nothing consumes the column yet, and the misuse the rename guards against is now blocked harder than a name would (comment: "NOT dividend-adjusted … NEVER a marking price"; `return_basis` refuses the mislabel at the point of damage). Recommend as its own one-line migration + mechanical rename PR before any consumer lands. |
| N-S2 | NIT | **FIXED** | Gap-band comment now names the misses: 3-for-2 caught, 5-for-4 and sub-33% stock dividends not — the structural limit of gap selection. |
| N-S3 | NIT | **FIXED** | `AD_HOC_CLOSURES` comment lists 9/11 (2001-09-11→14) and Sandy (2012-10-29/30) as mandatory additions if the calendar range ever moves earlier. |

**PRAISE items preserved:** 006's factor-not-level design is untouched and is now the documented
reason stored returns are PIT-safe; the verified split arithmetic (ROUND(exp(sum(ln)),12)) is
carried verbatim into `split_factor_between` — the only semantic change is the upper bound; the
resumability core, string COPY path, `synchronous_commit` reasoning, calendar-from-rules decision,
and the half-day "fortunate accident" are all intact (the last is now documented rather than
accidental).

---

## Revert evidence — every BLOCKER's test proven to catch its bug

Each defence was reverted on a scratch basis (file backed up, targeted revert applied, named test
run, file restored byte-identical; restores confirmed by `git diff`):

| ID | Named test (db/tests/test_loaders_db.py) | Revert applied | Observed result |
|---|---|---|---|
| B-1 | `test_b1_provider_failure_fails_the_run`, `test_b1_total_failure_short_circuits_as_connection_error` | `cmd_fetch`'s `except ProviderError` restored to `logger.debug(...); continue` and the non-zero exit removed (the old silent-swallow semantics) | **both FAILED** (run exited 0, no warning logged) |
| B-2 | `test_b2_corrupt_members_are_skipped_reported_and_nonzero` | `CORRUPT_STREAM_ERRORS = ()` in `load_minute_bars.py` (no translation → old behaviour) | **FAILED** with the raw `gzip.BadGzipFile` traceback — the exact pre-fix failure mode |
| B-S1 | `test_bs1_post_asof_splits_are_excluded` | `AND ex_date <= p_as_of` removed from 007's function | **FAILED**: factor `0.020000000000`, adj_close `104.0000000000` for the $1.04 close — the PAVS contamination reproduced exactly |
| B-S2 | `test_bs2_return_basis_is_required_and_closed` | `return_basis` NOT NULL dropped in 007 | **FAILED** (the unlabelled insert stored) |
| B-S3 | `test_bs3_bs4_catalog_comments_are_true` | 007's corrected `COMMENT ON` for `price_bars_daily.close` and `paper_portfolio_positions` stripped (004/002 texts remain) | **FAILED** (catalog reverted to the untrue formula/claim) |
| B-S4 | `test_bs4_daily_close_is_the_1559_bar_by_design` | `SESSION_LAST_MINUTE = dtime(16, 0)` — the exact change the review forbids | **FAILED** (stored close became the 16:00 bucket's post-close print 544.30). NB: the pin's first draft derived its bucket timestamps from `session_bounds_ns` and moved with the constant — caught during revert-proofing and re-anchored to absolute ET wall-clock times; the revert was then re-run red and the restore re-run green. |
| B-S5 | `test_bs5_splice_and_infer` | splice gap detection neutered (`WHERE FALSE AND …`) | **FAILED** (`assert 1 == 2` — the recycled ticker stayed one identity) |

---

## Live database repair (rh-db), in the review's required order

1. **007 applied** — `db_migrate.sh status`: 001–007 all `applied`, checksum `ok`.
2. **splice** — 289 identities split at the 180-day threshold (283 in pass 1 = the review's
   count; passes 2–3 caught multiply-recycled tickers, e.g. RIVr → three identities), 0 skipped,
   all actions re-attributed to current holders. Extended to `--min-gap-days 120` after the META
   discovery (see B-S5 row): +123 identities.
3. **fmp** — endpoint reachable; free tier = page 0 only (HTTP 402 beyond, observed live); 99
   recent records, 0 historical matches (all post-archive). Recorded in `data_sources`.
4. **infer** — 7,621 securities marked delisted by absence (cutoff 2025-09-25); a re-run after
   the 120-day splice found 0 further (splice sets `delisted_at` itself).
5. **adjust** re-run under the as-of bound (deliberately AFTER the B-S1 bound landed, per the
   review's warning against re-baking lookahead): 527 post-as-of splits excluded (the review's
   exact count), 1,650,416 + 1,648,849 bars adjusted across the two runs, 6,595 + 1,156
   stale-factor bars reset, 0 NULL factors, exit 0.
6. **rates** — DGS3MO: 11,225 observations 1981-09-01 → 2026-07-28 loaded, 0 revisions
   (revision path exercised in unit tests; FRED served no revisions today).

### Post-repair verification (all run against the live rh-db)

| Check | Result |
|---|---|
| `db_migrate.sh status` | 001–007 applied, checksum ok |
| `rh-db` container | running, healthy; `km-*` stack untouched (all Up/healthy) |
| Row counts | `price_bars_daily` 12,840,439 (unchanged — nothing truncated); `corporate_actions` 46,934 (unchanged); `market_calendar` 2,557 (unchanged); `securities` 19,713 (19,301 + 289 + 123 spliced identities); delisted 8,033; `risk_free_rates` 29,358 (18,133 DTB3 retained + 11,225 DGS3MO) |
| PAVS 2025-09-30 (the review's worked example) | close 1.04, adj_close **1.0400000000**, factor 1 — was adj_close 124,800. WOK 0.0748 (was 74,800), AREB 0.96 (was 38,400) |
| Factor PIT cross-check (`verify` + `verify_daily_series` check 5) | **PASS** *(claim corrected in round 2: the check as then written recomputed only the 1,648,849 bars of split-bearing securities — 12.8% of the table — while this row said "every one of 12.8M". Round 2 extended both tools to also verify the other 11,191,590 bars carry factor exactly 1, making the whole-table claim true.)* 527 post-as-of splits on record, all excluded |
| Cross-gap "returns" (>180d, the FLY +255% / FLXN +174% class) | **0** — unrepresentable after the splice |
| $5-floor query on future-split securities (review's demonstration) | 196,909 → 82,510 bars; the factor cross-check proves the remainder contains **zero** post-archive-split influence — the residual is IN-window reverse-split level effects, which is the documented stored-level caveat (levels at a historical decision date T require `split_factor_between(…, T)`; the stored column is pinned to archive end) |
| `adj_close` NULL / max level | 0 NULL; max 2.658e8 (was 2.63e12 — the absurd levels were themselves largely lookahead artefacts) |
| `verify_daily_series.py` (offline: alignment + factor PIT) | **all checks passed** — 20/20 reference symbols align exactly with the calendar minus the 15 registered gap sessions (this check caught META mid-verification; after the 120-day splice it passes) |
| `load_corporate_actions.py verify` | exit 0; residual-gap report generated (21,868 gaps / 4,213 securities — visibility, per design) |

**Known residuals, stated rather than buried:** the December-2024 15-session hole remains a data
gap (needs re-copied source files — S-S1's coverage machinery now makes it undeclarable in any
metric row); dividend coverage remains ~5% pending the `--candidates all` run (B-S2 schema guard
in force meanwhile); delisted identities' corporate actions are unfetchable from yfinance;
recycled tickers with gaps under 120 days remain possible (the alignment check is the tripwire);
securities metadata (type/exchange/sector) is still unpopulated.

---

## Gates

`bash bin/local_test.sh` — full run, CI-pinned containers, after all changes:

```
  PASS   HARD  ruff
  PASS   HARD  screen
  PASS   HARD  backend
  PASS   HARD  shellcheck
  PASS   HARD  database      (148 tests: 117 runner/discovery + 22 loader-unit + 9 blocker-regression)
  PASS   HARD  frontend

✓ all hard gates passed
```

Note: the database suite was RED on arrival (the 001–004 classification pin predated 005/006);
it is green now with the pin at 001–007.
