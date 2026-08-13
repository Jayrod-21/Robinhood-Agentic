# Review: Phase A financial and data semantics

**Reviewer:** independent senior review — quantitative-finance / market-data scope. I did not write
this code.
**Date:** 2026-07-29
**Question asked:** not "is this valid Python/SQL" (another reviewer owns that) but **does this data
mean what the project claims it means, and would a backtest built on it be honest?**

**Scope:** `db/load_daily_bars.py` (the session decision), `db/load_corporate_actions.py` (split
adjustment, candidate selection), `db/load_reference_data.py` (calendar rules, risk-free rate),
migrations `005_corporate_actions.up.sql` and `006_split_factor.up.sql` (the adjustment model), read
against `docs/EVALUATION_FRAMEWORK.md` §2/§3.5, `SENIOR_ENGINEER_BAR.md` §7.2, and
`docs/DATA_INVENTORY.md`.

**Method:** every load-bearing claim below was executed against the live `rh-db` (12,840,439 daily
bars, 19,301 securities, 3,889 splits, 43,045 dividends, 2,557 calendar days, 18,133 rates) and, where
provider comparison was needed, against yfinance and FRED from the egress container. Trade-level
claims were checked against the raw Polygon minute files on disk. **No data was modified** — every
statement was a `SELECT`; the two long-running read-only queries I started were cancelled with
`pg_cancel_backend`; `rh-db` is `running healthy` and the working tree is clean.

---

## Summary verdict: REQUEST CHANGES

**Five blockers.** The split arithmetic is right — I verified it against an independent provider and
it agrees to a fraction of a basis point. The *semantics wrapped around it* are not.

The two that matter most:

**B-S1 — `adj_close` and `split_adj_factor` contain future information.** `split_factor_after()`
multiplies every split with `ex_date > p_date` with no upper bound, and yfinance returns splits
through *today* (2026-07-29) while the archive ends 2025-10-02. 527 splits across 436 securities have
an ex-date after the archive window; 308,709 bars carry a factor computed from them. The return
series survives this (the factor cancels), but every price **level** is contaminated, and the
contamination is concentrated in exactly the worst names in the universe: 196,909 bars across 357
securities have a raw close below \$5 and an `adj_close` at or above \$5 purely because the company
reverse-split *after* any possible decision date. PAVS closed \$1.04 on 2025-09-30 and its stored
`adj_close` is \$124,800. A "no sub-\$5 stocks" screen run at a 2025 decision point admits it. That is
lookahead that *manufactures* apparent alpha, and `EVALUATION_FRAMEWORK` §3.5 says this class of
error must be structurally impossible, not merely unlikely.

**B-S2 — 005's dividend argument is sound in principle and unfunded in practice.** The reasoning
("dividends become cash at marking, so adjusting the price too would double-count") is correct. But
dividends exist for **960 of 19,301 securities (5.0%)**. SPY, QQQ, IWM, VOO, KO, T, VZ, XOM, MO, PFE,
JNJ, JPM, MSFT, O and MAIN all have **zero** dividend rows. There is nothing for the marking job to
credit, so the system computes a price-only return and stores it in a column named `total_return`.
The bias is not small and not random: 1.62 pp/yr for SPY, 9.05 for MO, 7.57 for T, 5.67 for VZ, 5.65
for XOM — i.e. it falls hardest on the high-FCF dividend-paying cohort that the Wasden screen is
built to select, and it biases the information ratio against the strategy by 3–7 pp/yr.

The other three: the stored daily close is not the official close and cannot be made so from this
archive (**B-S4**, adjudicating the peer review's S-6); the documented marking formula mixes two
share bases (**B-S3**); and ticker reuse splices two different issuers into one `security_id` for 283
securities (**B-S5**).

None of this is a competence problem. The migration headers reason carefully and mostly correctly;
what has happened is that each header reasons about *its own* step and no document reasons about the
composition. Three of the five blockers live precisely in the seams.

---

## Claim audit

| Claim made | Verdict | Evidence |
|---|---|---|
| 005: `adj_close` must be split-adjusted but NOT dividend-adjusted, because the marking job credits dividends to cash and doing both double-counts | **Correct premise, broken in composition** | Argument is right for a cash-accounting simulator. But (a) dividends exist for 5.0% of securities so nothing is credited (B-S2); (b) marking at a back-adjusted level with as-traded share counts is wrong by the split factor (B-S3); (c) nothing labels the result price-only, and `evaluation_runs.total_return` is the column it lands in |
| 005: `adj_close(t) = close(t) ÷ Π(split_ratio for every split with ex_date > t)` | **Arithmetically implemented exactly as stated — and that is the bug** | Verified: NVDA 2021-07-19 factor 40.0, 2021-07-20 factor 10.0, adj 18.78 → 18.61 = −0.93%. But "every split with ex_date > t" is unbounded above, so post-window splits enter (B-S1) |
| 005: split-adjustment keeps the series continuous; NVDA −75.23% artefact becomes −0.93% | **TRUE — verified independently** | `adj_close` vs yfinance split-adjusted close: NVDA mean −0.024 bps (n=1241), GOOGL +0.091, AMZN +0.024, WMT +0.134, TSLA −0.534. Residual sd 2.7–12.9 bps is the close-definition noise of B-S4, not adjustment error |
| 005: rows with NULL `announced_at` "must be excluded from any point-in-time claim" | **STATED, NOT ENFORCED** | `announced_at IS NOT NULL` on **0 of 46,934** actions; `split_factor_after()` does not reference the column. Every adjustment in the database is built from rows the migration itself says are unusable point-in-time |
| 006: the factor is the stable representation because the large intermediate cancels in returns | **TRUE and genuinely well-reasoned** | Correct, and it is why B-S1 damages levels but not returns. Best single insight in the two migrations |
| 006: `adj_close` "is left NULL for the pathological ones rather than the load failing"; "a NULL means the level is not representable — use the factor" | **FALSE in the loaded data** | `count(*) WHERE adj_close IS NULL` = **0**. The guard is `< 1e19` against a `NUMERIC(30,10)` that holds < 1e20, so it never fires. ADTX stores `adj_close = 2,630,000,000,000.00`; WHLR 58,260,869,565; PAVS 124,800. The documented fail-loud path does not exist; a silent absurd number takes its place (S-S9) |
| `load_daily_bars`: 09:30–15:59 ET is "the convention every daily OHLCV feed uses" | **FALSE** | The convention is the official closing-auction price. That print lands in the **16:00** minute bucket, which is excluded. Only 6.8% of 1,241 SPY closes match yfinance to <\$0.005 (B-S4) |
| `load_daily_bars`: `close = the close of the LAST regular-session minute` | TRUE as literally implemented; the caveat is that this is not what "daily close" means anywhere else | See B-S4 |
| `load_daily_bars`: DST handled via `America/New_York` rather than a fixed offset | **TRUE** | Session bounds go through `ZoneInfo`; verified against both EST and EDT files |
| `load_reference_data`: the calendar must not be derived from data coverage | **TRUE, and the most important correct decision in Phase A** | It is the only reason the 15-session hole is knowable at all. Verified: `report`'s join returns exactly 2024-12-10 … 2024-12-31 |
| `load_reference_data`: NYSE holiday/early-close rules match reality | **TRUE for the window** | 1,759 trading days; 14 early closes; all 10 in-window early closes correct, including the `_observed_new_year` exemption that keeps 2021-12-31 open |
| `load_reference_data`: rates stored as fractions (0.0525 = 5.25%) | **TRUE** | `annual_rate` range 0.0001–0.0536 in window; ±1 CHECK holds |
| `load_reference_data`: `known_at = effective_date + 1 day` is "very slightly optimistic" | **Actually CONSERVATIVE on the lag question** | H.15 publishes DTB3 ~16:15 ET on day D; `known_at` = D+1 00:00 UTC ≈ 19:00–20:00 ET on day D, i.e. *after* publication. The docstring is unduly pessimistic about itself |
| `risk_free_rates`: "known_at is part of the identity — a revision is a new row" | **FALSE as loaded** | `known_at` is a pure function of `effective_date`, so a revision collides on the PK and `ON CONFLICT DO NOTHING` discards it. 18,133 rows / 18,133 distinct `(series, effective_date)` — `known_at` adds zero discriminating power (S-S4) |
| 004 comment names `'DGS3MO'` as the example series | **Loader uses DTB3 instead** — different quotation basis | DGS3MO − DTB3 = **+0.1177 pp** mean over the window, **+0.2121 pp** mean over 2023-01…2024-06, max +0.28 pp. One-directional overstatement of every excess return (S-S3) |
| `load_corporate_actions`: gap selection is "high-coverage, NOT complete" | **TRUE, and the honesty is appreciated — but the residual is larger than the tone implies** | Sampled `security_id` 1–4000: 488 securities have a >40% single-session gap in the *adjusted* series with prior raw close ≥ \$1, and **346 (71%) have no split recorded at all** (S-S5) |
| `securities.delisted_at` retention model: "a symbol re-appearing after its previous holder was delisted is a NEW row" | **Model documented, loader never implements it** | `delisted_at` NULL on 19,301/19,301; 283 securities have a >180-day internal hole = two issuers in one id (B-S5) |
| "SPY +100.4% over the window validates the pipeline" | **The number is right; the test is far too coarse to validate anything** | Ours 100.428% vs official 100.46% (3 bps). But a **20-session** start offset yields 104.93% and a 1-session offset 96.97% — both inside any tolerance you would set on "≈100%" (S-S6) |
| `n_observations` counts SESSIONS (framework §2 rule 3) | **It counts MARKS, and nothing compares marks to sessions** | The trigger checks n against the mark count; `ck_evaluation_runs_n_window` bounds n by *calendar* days. Neither can see a 15-session hole (S-S1) |

---

## Findings

### BLOCKER

- **B-S1** — `005:120-138` (`split_factor_after`) + `load_corporate_actions.py:143-148`: the split
  product has **no upper bound on `ex_date`**, so splits with an ex-date after the archive window
  contaminate every historical price *level*. 527 splits / 436 securities / 308,709 bars.
  196,909 bars across 357 securities pass a \$5 price floor on `adj_close` that they fail on `close`.
  Violates `EVALUATION_FRAMEWORK` §3.5 ("a feature may only use rows whose timestamp is ≤ the
  decision time. Enforced in the feature store, not by convention") and Bar §7.2 P1 "no lookahead
  (point-in-time data)". Aggravated by `announced_at` being NULL on 100% of actions while 005's own
  comment forbids using such rows for a point-in-time claim.

- **B-S2** — `load_corporate_actions.py:94-112` + `005:107-110`: dividend coverage is **960 / 19,301
  securities (5.0%)** because dividends are only fetched for gap-selected candidates, and a
  dividend payer with no split has no gap. SPY has zero dividend rows. 005's double-counting
  argument is therefore load-bearing on data that does not exist, and the system will compute
  price-only returns and store them in `evaluation_runs.total_return`. Measured drag over the
  window: SPY 1.62 pp/yr, MO 9.05, T 7.57, VZ 5.67, XOM 5.65, PFE 4.62, KO 3.26, JNJ 3.04.
  Strategy-correlated, not random.

- **B-S3** — `004:480-484` and `005:152-155`: the documented marking rule is `Σ shares × adj_close +
  cash`, but `adj_close` is expressed on the **current** share basis while
  `paper_portfolio_positions.shares` / `entry_price` are as-traded (`entry_price NUMERIC(18,6)` is a
  raw fill price). Any lot held across a split is marked wrong by the split factor — 40× for an NVDA
  lot opened before 2021-07-20, ~1/120,000 for a PAVS lot. `entry_price NUMERIC(18,6)` (max ≈1e12)
  cannot even hold the largest stored `adj_close` (2.63e12), and `market_value NUMERIC(18,2)`
  overflows shortly after. This is the direct downstream cost of 005's decision to keep a
  back-adjusted level as the marking price; 005 half-acknowledges it ("fills happened at the raw
  close") without resolving it.

- **B-S4** — `load_daily_bars.py:82` (`SESSION_LAST_MINUTE = 15:59`): **the stored close is not the
  official close, and it cannot be recovered from this archive.** The closing-auction print is
  stamped inside the 16:00 ET minute bucket, which the loader excludes by design — and its position
  within that bucket is not fixed, so no bucket field recovers it. Verified at trade level:
  MSFT 2023-12-15 official close 370.73 = the **open** of the 16:00 bar (stored 368.77, −52.9 bps);
  SPY 2025-04-09 official 548.62 = the **high** of the 16:00 bar (stored 543.37, −95.7 bps). Only
  6.8% of 1,241 SPY closes match to <\$0.005. Asymmetric: the *opening* auction IS captured (the
  09:30 bucket is inside the window), so `open` matches to the cent while `close` does not. Volume is
  systematically low — median ours/official 0.853 for SPY — which will understate any ADV, liquidity
  or slippage model. **Adjudicates the peer review's S-6, which was routed here for a domain ruling.**

- **B-S5** — `load_daily_bars.py:270-284` (`resolve_symbols`) + `001` `uq_securities_symbol_live`:
  `delisted_at` is never set, so the schema's own re-listing rule never fires and **283 securities
  are two different issuers under one `security_id`**. Worked examples (last bar → first bar after a
  >365-day hole): TOT \$48.27 → \$20.02, FLY \$17.03 → \$60.51 (+255%), FLXN \$9.14 → \$25.05 (+174%),
  PCI \$20.47 → \$50.32 (+146%), GLIBA \$91.67 → \$30.87 (−66%). 22 of the 283 also carry splits, so
  the *current* issuer's split history is being applied to the *prior* issuer's prices. Separately,
  7,621 securities (39.5%) have no bar after 2025-09-25 with no delisting record, so a backtest has
  no way to force an exit or mark a dead position to its recovery value.

### SHOULD-FIX

- **S-S1** — `004:673-740` + `004:795-818`: nothing connects a metric window to `market_calendar`.
  `ck_evaluation_runs_n_window` bounds n by *calendar* days and the trigger checks n against the mark
  count; both are satisfied by a series with a 15-session hole. Measured on SPY's own archive series:
  the 2024-12-09 → 2025-01-02 hole appears as a single −3.321% "daily" return that is really a
  16-session move, taking Sharpe from 0.7944 to 0.7514 (−5.4% relative) and annualised mean from
  16.289% to 15.601%. The framework says `market_calendar` exists "so a gap in the marks is
  distinguishable from a holiday" — it is, by a query nobody is obliged to run.

- **S-S2** — `004:695-698`: the schema stores `risk_free_annual`, `periods_per_year` and
  `return_frequency` but **not the conversion rule**, so it does not deliver the comparability it
  promises. On the real SPY series with the window's mean DTB3 (3.0563%): rf/252 → 0.7747,
  rf/365 → 0.8301 (+7.1%), compounded `(1+r)^(1/252)−1` → 0.7773, rf omitted → 0.9536, rf mistaken
  for a percent → −16.94. At 5.25% the /252-vs-/365 choice is a 1.63 pp/yr difference in the rf.

- **S-S3** — `load_reference_data.py:73`: DTB3 is a **discount-basis** rate; a Sharpe's rf should be
  on the investment/coupon-equivalent basis, which is what the 004 comment's own example
  (`'DGS3MO'`) names. Measured spread DGS3MO − DTB3: +0.1177 pp mean over the window, +0.2121 pp
  mean over the 5%+ regime, max +0.28 pp — a one-directional overstatement of every excess return.
  Free to fix: change one constant.

- **S-S4** — `load_reference_data.py:273` + `004:86`: `known_at` derived deterministically from
  `effective_date` makes revisions unrepresentable (PK collision → `DO NOTHING` → silent discard).
  Use the FRED fetch time (or the vintage from ALFRED) as `known_at`; keep `effective_date + 1d` only
  as a *floor* for the PIT predicate.

- **S-S5** — `load_corporate_actions.py:67` and `346-422`: residual split exposure is larger than the
  docstring's tone conveys. Over `security_id` 1–4000, 488 securities show a >40% single-session gap
  in the **adjusted** series above the \$1 price floor and 346 of them (71%) have no split recorded.
  Pro-rating to the 2,347 flagged securities gives ≈1,660. `verify`'s round-ratio filter finds only
  the subset that looks like 2/3/4/10-for-1 and persists 5 sessions. **Conclusion: this data supports a
  curated watchlist backtest, not a universe backtest.** Say so in `DATA_INVENTORY.md`, which
  currently says the price side is "genuinely sufficient".

- **S-S6** — the "+100.4% validates the pipeline" test is too coarse (see Detailed §8). Replace with
  the per-session test proposed there.

- **S-S7** — `securities`: `name`, `exchange`, `security_type`, `sector`, `industry` are **100% NULL**,
  so no universe query can exclude non-common-stock instruments — the archive holds 137 warrant-form,
  88 rights-form, 680 preferred-form and 900 dotted symbols. `first_seen` = 2020-10-02 for 8,678
  securities is **left-censoring**, not a listing date, and 001's comment only warns about the NULL
  case.

- **S-S8** — `006:39-46`: `split_adj_factor` has no `NOT NULL` and no trigger. Incrementally loaded
  bars arrive with a NULL factor until `adjust` is re-run by hand, and nothing stops a consumer
  computing returns off `close`. Combined with the peer review's B-1 (provider errors silently
  becoming "no actions", then `adjust` stamping `factor = 1`), this is the live corruption path.

- **S-S9** — `load_corporate_actions.py:277-280` + `006:53-58`: the `< 1e19` guard never fires against
  `NUMERIC(30,10)`, so 006's documented "NULL means not representable — treat as an error" contract
  is dead code and absurd levels are stored silently instead. Extends the peer review's N-6 from a
  wrong comment to a wrong behaviour.

### NIT

- **N-S1** — `adj_close` collides with the near-universal industry meaning (Yahoo, CRSP, and every
  mainstream backtest library treat "adjusted close" as **total-return** adjusted; yfinance's
  `auto_adjust=True` is the default). 005 chose to redefine the conventional name rather than pick an
  unambiguous one. `split_adj_close` costs nothing and the sibling `split_adj_factor` already models
  the naming.
- **N-S2** — `GAP_LOW/GAP_HIGH = 0.75 / 1.3333` catches 3-for-2 (1.5) but misses 5-for-4 (1.25) and
  every stock dividend below 33%. Documented for the 1.1 case; worth stating the 1.25 case too.
- **N-S3** — `AD_HOC_CLOSURES` holds one entry. Worth a comment that 2001-09-11…14 and 2012-10-29/30
  are out of the current calendar range but must be added if `--from` ever moves earlier.

### PRAISE

- **The split arithmetic is correct, and I tried to break it.** `adj_close` matches yfinance's
  independently-computed split-adjusted close with a mean error of −0.024 bps on NVDA (two splits,
  cumulative 40×), +0.024 on AMZN, +0.091 on GOOGL, +0.134 on WMT. The `ex_date > p_date` boundary is
  exactly right — the bar *on* the ex-date already trades on the new basis and is correctly left
  unadjusted. The `ROUND(..., 12)` fix for `exp(sum(ln))` is the right call for the right reason.
- **006's "the factor, not the level" insight is the strongest piece of reasoning in Phase A.** It is
  correct, it is why B-S1 spares the return series, and it generalises to the 30-year FMP history as
  claimed.
- **Refusing to derive the calendar from data coverage.** This is the decision that makes the whole
  archive auditable; had it gone the other way, the 15-session hole would be permanently invisible and
  every one of my §3 measurements would have been impossible. The `_observed_new_year` special case,
  caught by testing rules against reality rather than trusting them, is exactly the right instinct.
- **A fortunate accident worth documenting rather than "fixing":** on the 14 early-close days Polygon
  emits nothing between 13:01 and 15:59 (verified for SPY/AAPL/KO on 2021-11-26, 2024-07-03,
  2024-11-29), and the 13:00 bucket is inside the window — so half-day closes have *better* fidelity
  than full-day closes. The peer review's S-6 read this as "empirically benign"; it is better than
  benign, and the asymmetry with full days is the tell that pointed at B-S4.
- Storing rf as a fraction with a ±1 CHECK, and carrying `rf`/`mar`/`periods_per_year` on
  `evaluation_runs` at all, is more discipline than most production quant stacks have. S-S2 is a
  missing field on a good design, not a bad design.

---

## Detailed findings

### 1. The split-only adjustment decision — auditing 005's reasoning

**The argument is correct as far as it goes.** For a simulator that credits dividends to cash, marking
positions at a dividend-adjusted price does double-count: the back-adjusted price already embeds the
distribution. Total-return-adjusted prices and cash-crediting are two mutually exclusive models of the
same cash flow, and 005 picks the one that matches a cash brokerage account. On that narrow question
the header is right and the conventional phrase in 002 was wrong.

**Three things break.**

**(a) There is nothing to credit.** The argument is only sound if dividends are actually credited.

```sql
SELECT (SELECT count(*) FROM securities)                                                    AS securities,
       (SELECT count(DISTINCT security_id) FROM corporate_actions
         WHERE action_type='cash_dividend')                                                 AS with_dividends;
-- 19301 | 960     (5.0%)

SELECT s.symbol, count(*) FILTER (WHERE ca.action_type='cash_dividend') AS divs
FROM securities s LEFT JOIN corporate_actions ca ON ca.security_id = s.id
WHERE s.symbol IN ('SPY','QQQ','IWM','VOO','KO','T','VZ','XOM','MO','PFE','JNJ','JPM','MSFT','O','MAIN')
GROUP BY 1 ORDER BY 1;
-- every one of them: 0
```

The mechanism is structural: dividends are fetched only for gap-selected candidates, and a dividend
payer without a split has no anomalous gap, so it is never asked about. AAPL (91 dividends) and NVDA
(55) are in the table only because they split.

**(b) The consumer that expects total-return prices is every mainstream library.** quantstats,
pyfolio, vectorbt, bt, backtrader's adjusted feeds and yfinance's default all treat "adjusted close"
as total-return adjusted. Pointed at this column they silently produce a price-only Sharpe. Measured
cost over 2020-10-02 → 2025-10-02:

| Symbol | price-only | total return | annualised drag |
|---|---|---|---|
| SPY | 100.46% | 115.03% | **1.62 pp/yr** |
| KO | 33.91% | 55.78% | 3.26 |
| JNJ | 27.17% | 46.67% | 3.04 |
| PFE | −21.54% | −0.59% | 4.62 |
| XOM | 237.45% | 319.17% | 5.65 |
| VZ | −26.74% | −1.78% | 5.67 |
| T | 24.69% | 76.89% | **7.57** |
| MO | 68.72% | 149.62% | **9.05** |

At SPY's 17.1% realised vol, 1.62 pp/yr is ≈0.095 of Sharpe. At MO's vol it is ≈0.4. And because the
drag scales with yield, it is **correlated with the strategy**: the Wasden lens selects high-FCF,
high-yield value names, so the evaluation harness will systematically rank the intended strategy below
a zero-yield growth strategy for a reason that has nothing to do with skill.

**(c) Yes — there is a place that will compute a price-only return and call it a total return.**
`evaluation_runs.total_return` and `annualized_return` (004:709-710) have no field distinguishing
price-only from total-return, and `information_ratio` vs `benchmark_security_id` (004:713-715) reads
SPY's bars, which carry no dividends either. Both legs being price-only makes the *level* consistent
but the *differential* biased by 3–7 pp/yr against a yielding book.

**What I would do:** (1) fetch dividends for the full universe, not gap candidates — this is the same
`--candidates all` run B-S1's fix requires anyway; (2) rename to `split_adj_close` (N-S1); (3) add
`return_basis TEXT NOT NULL CHECK (return_basis IN ('price_only','total'))` to `evaluation_runs`, so a
price-only Sharpe cannot be stored as if it were a total-return one; (4) keep the cash-crediting
model — it is the right one — and make the marking job fail loudly when it marks a position in a
security with dividend coverage it does not have.

### 2. The regular-session-only daily bar — verified empirically, and it is wrong

The header's defence ("which is the convention every daily OHLCV feed uses") is the part that does not
hold. The convention is the **official closing price**, which is the closing-auction cross, and that
print lands in the 16:00 ET minute bucket that `SESSION_LAST_MINUTE = 15:59` excludes.

Provider comparison, 1,241 paired SPY sessions (yfinance, `auto_adjust=False`):

```
|diff| > 0.5 bps :   781 / 1241  (62.9%)      mean signed diff  −0.165 bps
|diff| >   1 bps :   386 / 1241  (31.1%)      level sd           2.99 bps
|diff| >   2 bps :    63 / 1241  ( 5.1%)      ann vol ours     17.082%  vs official 17.331%
|diff| >   5 bps :    11 / 1241  ( 0.9%)      volume ratio ours/official: median 0.853
|diff| >  20 bps :     1 / 1241  ( 0.1%)                                   min 0.627, max 0.995
```

Only **85 of 1,241 (6.8%)** closes match to under half a cent. The *mean* is benign — this is not a
systematic few-basis-point bias, which is the good news — but the tail is not, and the root cause is
worse than a fixed offset. From the raw archive (`data/market/minute_bars_5y/2025/04/2025-04-09.csv.gz`
and `.../2023/12/2023-12-15.csv.gz`):

```
SPY 2025-04-09 ET     o        c        h        l         vol
  15:59            543.59   543.37   544.56   542.66   4,637,786   <- stored close = 543.37
  16:00            543.35   544.30   548.62   541.80   1,224,688   <- official close 548.62 = the HIGH

MSFT 2023-12-15 ET
  15:59            370.07   368.77   370.08   367.21   2,388,591   <- stored close = 368.77
  16:00            370.73   369.93   370.73   369.01     570,724   <- official close 370.73 = the OPEN
```

**The auction print's position inside the 16:00 bucket is not fixed** — it is the high on one day and
the open on another, because the bucket also contains post-close continuous prints. So there is no
rule over 1-minute aggregates that recovers the official close. This is not a one-line fix; it is a
data-source question:

- Polygon's daily aggregates endpoint (`/v2/aggs/ticker/{t}/range/1/day/...`) returns the official
  close directly — the cheapest correct answer, one call per symbol-range.
- Trade-level data with condition codes (the official-closing-price condition) is the fully correct
  answer and is much more expensive.
- Staying with 15:59 is defensible *if* the column is renamed or documented as
  `close_1559_et` and every reconciliation is understood to differ. It is not defensible while the
  docstring claims it is the standard convention.

Two consequences beyond the close itself. First, the asymmetry: the *opening* auction IS captured
because the 09:30 bucket is inside the window, so `open` matches the official open to the cent while
`close` does not — an open-to-close return therefore mixes two different measurement conventions.
Second, volume is ~15% low for SPY (median ratio 0.853) because both extended hours *and* the closing
cross are excluded; any ADV filter, participation-rate cap or slippage model calibrated on this will
understate available liquidity.

Peer coordination: `REVIEW_phaseA_loaders.md` S-6 raised the early-close inconsistency and explicitly
deferred the domain ruling. **Ruling: BLOCKER, and the early-close half of it is fine.** I confirmed
that Polygon emits no SPY/AAPL/KO bars between 13:01 and 15:59 on 2021-11-26, 2024-07-03 and
2024-11-29, so the 13:00 bucket is the last one and half-days capture the auction window. The
problem is the 1,745 full days, not the 14 half days.

### 3. `n_observations`, the calendar, and what a Sharpe across the hole means

The framework requires n to count sessions. It counts marks, and the two enforcement mechanisms are
both blind to the difference:

- `ck_evaluation_runs_n_window CHECK (n_observations <= window_end - window_start + 1)` bounds n by
  **calendar** days — 92 for a quarter, against ~63 sessions. It cannot detect a 15-session shortfall.
- `enforce_eval_run_n_observations()` compares n to `count(*) FROM portfolio_returns_daily` — i.e. it
  verifies that the claim matches the marks that *exist*. If the marks are missing, n is "correct".

Nothing joins either to `market_calendar`. The hole:

```sql
SELECT c.trade_date
FROM market_calendar c
WHERE c.is_trading_day
  AND c.trade_date BETWEEN (SELECT min(trade_date) FROM price_bars_daily)
                       AND (SELECT max(trade_date) FROM price_bars_daily)
  AND NOT EXISTS (SELECT 1 FROM price_bars_daily d WHERE d.trade_date = c.trade_date);
-- 2024-12-10 … 2024-12-31, 15 sessions
```

**What the gap actually becomes.** It is not a zero-return day and not a skipped day — under the
natural `lag()` marking it becomes **one enormous "daily" return**:

```sql
WITH r AS (
  SELECT d.trade_date, d.adj_close,
         lag(d.adj_close)  OVER (ORDER BY d.trade_date) AS prev,
         lag(d.trade_date) OVER (ORDER BY d.trade_date) AS prev_date
  FROM price_bars_daily d JOIN securities s ON s.id = d.security_id
  WHERE s.symbol = 'SPY'
)
SELECT trade_date, prev_date, (trade_date - prev_date) AS calendar_gap,
       round((adj_close/prev - 1) * 100, 3) AS ret_pct
FROM r WHERE trade_date BETWEEN '2024-12-05' AND '2025-01-08' ORDER BY 1;

-- 2024-12-09 | 2024-12-06 |  3 | -0.530
-- 2025-01-02 | 2024-12-09 | 24 | -3.321   <- 16 sessions compressed into one observation
-- 2025-01-03 | 2025-01-02 |  1 |  1.247
```

−3.321% is the largest single "daily" loss in SPY's stored series for that period and it never
happened. Cost on the full five-year series:

```sql
WITH r AS (SELECT d.trade_date, d.adj_close,
                  lag(d.adj_close)  OVER (ORDER BY d.trade_date) prev,
                  lag(d.trade_date) OVER (ORDER BY d.trade_date) prev_date
           FROM price_bars_daily d JOIN securities s ON s.id=d.security_id WHERE s.symbol='SPY'),
     g AS (SELECT (adj_close/prev - 1)::float8 ret, (trade_date - prev_date) gap
           FROM r WHERE prev IS NOT NULL)
SELECT 'gap included' AS series, count(*) n,
       round((avg(ret)*252*100)::numeric, 3)                                       ann_mean_pct,
       round((stddev_samp(ret)*sqrt(252)*100)::numeric, 3)                         ann_vol_pct,
       round(((avg(ret)*252 - 0.0306)/(stddev_samp(ret)*sqrt(252)))::numeric, 4)   sharpe
FROM g
UNION ALL SELECT 'gap excluded', count(*), round((avg(ret)*252*100)::numeric,3),
       round((stddev_samp(ret)*sqrt(252)*100)::numeric,3),
       round(((avg(ret)*252 - 0.0306)/(stddev_samp(ret)*sqrt(252)))::numeric,4)
FROM g WHERE gap <= 4;

-- gap included | 1240 | 15.601 | 17.143 | 0.7514
-- gap excluded | 1239 | 16.289 | 17.082 | 0.7944
```

**0.7514 vs 0.7944 — a 5.4% relative error in Sharpe from one hole, on the benchmark itself.** The
annualisation is also wrong in a second way: `periods_per_year = 252` scales a series whose 1,240
observations span 1,256 sessions of calendar time.

**Fix:** a trigger (or a required `expected_n` column) that compares `n_observations` against
`count(*) FROM market_calendar WHERE is_trading_day AND trade_date BETWEEN window_start AND
window_end`, and refuses — or at minimum records a `coverage_ratio` — when they differ. A stored ratio
is enough; a silent shortfall is not.

### 4. The risk-free rate — conversion, basis, and materiality

**Conversion is documented nowhere.** `004:695-698` stores `risk_free_annual`, `periods_per_year=252`
and `return_frequency='daily'`, which strongly *implies* rf/252, but implication is not a spec, and the
migration's own stated purpose for those columns is that "any two rows are comparable". They are not,
because the conversion rule is not a column. Measured on the real SPY series, mean in-window DTB3 =
3.0563%:

| Convention | Sharpe |
|---|---|
| rf/252 per day | 0.7747 |
| rf/365 per day | **0.8301** (+7.1%) |
| compounded `(1+rf)^(1/252) − 1` | 0.7773 |
| rf subtracted at the annual level | 0.7747 |
| rf forgotten | 0.9536 |
| rf read as a percent, not a fraction | −16.94 |

At 5.25% the /252-vs-/365 choice is a 1.63 pp/yr difference in the rf itself (2.083 bps/day vs 1.438
bps/day). Compounding vs simple division is immaterial (2.031 vs 2.083 bps/day) and can be ignored;
the day-count cannot. The percent/fraction trap is already closed by the ±1 CHECK and the explicit
column comment — good.

**The stored value is the right *shape* (a simple annual fraction) but the wrong *series*.** DTB3 is
"3-Month Treasury Bill, Secondary Market Rate, **Discount Basis**". A discount rate is quoted on face
value over actual/360; a Sharpe's rf should be an investment (coupon-equivalent) yield. The 004 comment
itself names `'DGS3MO'` — the constant-maturity series quoted on an investment basis — as the example;
the loader hardcodes DTB3. Measured directly from FRED:

```
overlap in archive window: 1250 days
  mean DTB3   3.0563%      mean DGS3MO 3.1740%
  mean(DGS3MO − DTB3) = +0.1177 pp      max +0.2800 pp      min −0.0200 pp
  during the 5%+ regime (2023-01…2024-06, n=374): mean +0.2121 pp, max +0.2800 pp
```

Analytically, `BEY = 365·d / (360 − 91·d)`: at d = 5.25% the coupon equivalent is 5.395%, +14.5 bps.
The empirical spread is larger still (constant-maturity vs secondary-market bill adds a little). So
**yes, the discount-vs-investment distinction matters here**: it understates rf by ~12–21 bps on
average, which overstates every excess return and every Sharpe, always in the same direction. It is
worth ~0.012 of Sharpe on SPY — small next to S-S1's 0.043 and B-S2's ~0.095, but it costs one line to
fix and it is the only one of the three that is a pure, free correction.

### 5. Point-in-time integrity of the rate

**On the lag question: conservative, and the docstring is harder on itself than it needs to be.**
`known_at = effective_date + 1 day` at UTC midnight = 19:00 ET (EST) / 20:00 ET (EDT) on the effective
date. FRED's H.15 publishes DTB3 at ~16:15 ET the same day, so the stored `known_at` is *after* actual
publication: a consumer enforcing `known_at <= decision_time` will decline to use day D's rate for a
decision at 15:00 ET on day D, which is correct behaviour. The docstring's "very slightly optimistic"
is wrong in the safe direction.

**The CHECK constraint interaction is fine but redundant.** `ck_risk_free_rates_known CHECK (known_at
>= effective_date::timestamp AT TIME ZONE 'UTC')` requires known_at ≥ D 00:00 UTC; the loader supplies
D+1 00:00 UTC, so there is 24 h of slack and the constraint never binds. It is a floor that would catch
a future loader claiming to know a rate before its date — worth keeping.

**The real defect is revisions, and it is the opposite of an approximation problem.** Because
`known_at` is a *pure function* of `effective_date`, the PK `(series, effective_date, known_at)`
collapses to `(series, effective_date)`, and `ON CONFLICT … DO NOTHING` therefore **silently discards
every revised value**:

```sql
SELECT count(*), count(DISTINCT (series, effective_date)) FROM risk_free_rates;
-- 18133 | 18133      known_at adds zero discriminating power
```

`004:75`'s comment ("rates get revised; this is the PIT anchor") and the loader's derivation are in
direct contradiction. Fix: set `known_at` from the fetch timestamp (or ALFRED vintage dates), keeping
`effective_date + 1d` as the constraint floor. Then a revision lands as the new row the schema
promises, and a point-in-time read is `ORDER BY known_at DESC LIMIT 1 WHERE known_at <= as_of`.

### 6. Survivorship bias, concretely

The naive universe query, and what it gets wrong:

```sql
-- What a backtest would write to pick a universe "as of 2021-06-30"
SELECT count(*) FROM securities
WHERE delisted_at IS NULL AND first_seen <= '2021-06-30';
-- 11725

-- Ground truth: securities that actually traded that week
SELECT count(DISTINCT security_id) FROM price_bars_daily
WHERE trade_date BETWEEN '2021-06-24' AND '2021-06-30';
-- 10773

-- Of the naive set: had no bar that week (not yet listed, or already gone)
-- 952   (8.1% wrongly INCLUDED)
-- Of the naive set: dead by the archive's end, with no delisting record
-- 5051  (43% of the universe are names whose death the schema cannot express)
```

Two distinct problems, and the first is *not* the classic one:

**(a) The archive does include delisted names — which is right — but nothing marks them dead.**
`delisted_at` is NULL on 19,301 / 19,301 (issue #39). 7,621 securities (39.5%) have no bar after
2025-09-25. A backtest holding one of those simply finds no bar and, depending on the marking job's
NULL handling, either forward-fills the last price forever (a position that never realises its loss —
this *overstates* returns, the survivorship error in its most expensive form) or fails to mark and
silently reduces `n_observations`. `portfolio_returns_daily.market_value` is `NOT NULL` with
`CHECK (market_value >= 0)`, so the marking job *must* choose, and nothing tells it what to choose.

**(b) Ticker reuse merges two issuers into one price series.** 001's comment specifies the correct
model — "a symbol re-appearing after its previous holder was delisted is a NEW row" — and
`resolve_symbols` never sets `delisted_at`, so it never happens.

```sql
WITH g AS (
  SELECT d.security_id, d.trade_date, d.close,
         lag(d.trade_date) OVER w prev, lag(d.close) OVER w prev_close
  FROM price_bars_daily d WINDOW w AS (PARTITION BY d.security_id ORDER BY d.trade_date)
)
SELECT s.symbol, g.prev last_before, g.trade_date first_after, (g.trade_date-g.prev) days_gap,
       round(g.prev_close,4) close_before, round(g.close,4) close_after,
       round((g.close/g.prev_close)::numeric,4) ratio
FROM g JOIN securities s ON s.id = g.security_id
WHERE g.prev IS NOT NULL AND (g.trade_date - g.prev) > 365 AND g.prev_close > 0
ORDER BY days_gap DESC LIMIT 8;

-- FTVw   2020-10-08 -> 2025-06-25  1721d   68.00 ->  55.93   0.8225
-- GLIBA  2020-12-18 -> 2025-07-15  1670d   91.67 ->  30.87   0.3368   -66%
-- TAPR   2020-11-30 -> 2025-04-01  1583d   77.00 ->  24.28   0.3153
-- CHA    2021-01-08 -> 2025-04-17  1560d   26.15 ->  32.26   1.2337
-- PLT    2021-05-21 -> 2025-08-19  1551d   31.85 ->  16.30   0.5118
-- TOT    2021-06-10 -> 2025-09-03  1546d   48.27 ->  20.02   0.4147   Total SA -> a new issuer
-- FLY    2021-08-02 -> 2025-08-07  1466d   17.03 ->  60.51   3.5531   +255%
-- FLXN   2021-11-18 -> 2025-07-10  1330d    9.14 ->  25.05   2.7407   +174%
```

283 securities carry an internal hole > 180 days. **22 of them also carry a recorded split**, meaning
the *current* holder's split history is being applied to the *previous* holder's prices — a wrong
adjustment layered on a spliced series.

**Quantified answer to "how many securities in the archive stopped trading before 2025-10-02":**
7,621 have no bar after 2025-09-25; 6,560 stopped before 2025-01-01; 3,133 stopped before 2023-01-01.
None of them is marked delisted.

**Fix:** in `resolve_symbols`, treat a symbol whose previous holder has been absent for N sessions as
a new `securities` row (which is what `uq_securities_symbol_live` was built for), and add a
`close_delisted` pass that sets `delisted_at` from the last bar plus a confirmation window. Until then
the honest statement in `DATA_INVENTORY.md` is that the archive supports point-in-time universes only
for names continuously present, and 283 series are known-spliced.

### 7. Candidate-selection completeness — residual exposure

Gap selection is correctly described as selection rather than detection, and `verify` is the right
instinct. The residual is bigger than the docstring's tone suggests.

Full-table scan of the **adjusted** series (excluding the December calendar gap, prior raw close ≥ \$1):

```sql
WITH r AS (SELECT d.security_id, d.trade_date, d.adj_close,
                  lag(d.adj_close)  OVER w prev,
                  lag(d.trade_date) OVER w prev_d,
                  lag(d.close)      OVER w prev_raw
           FROM price_bars_daily d WINDOW w AS (PARTITION BY d.security_id ORDER BY d.trade_date)),
     g AS (SELECT security_id, (adj_close/prev)::float8 ratio, prev_raw
           FROM r WHERE prev IS NOT NULL AND prev > 0 AND (trade_date - prev_d) <= 5)
SELECT count(*) FILTER (WHERE ratio < 0.6 OR ratio > 1.6667)                          gaps_40pct,
       count(DISTINCT security_id) FILTER (WHERE ratio < 0.6 OR ratio > 1.6667)       secs_40pct,
       count(*) FILTER (WHERE (ratio<0.6 OR ratio>1.6667) AND prev_raw >= 1)          gaps_over_1usd,
       count(DISTINCT security_id) FILTER (WHERE (ratio<0.6 OR ratio>1.6667) AND prev_raw>=1) secs_over_1usd,
       count(*) FILTER (WHERE ratio < 0.34 OR ratio > 2.9)                            gaps_3x
FROM g;
-- 19624 | 4075 | 4072 | 2347 | 3607
```

Sampled over `security_id` 1–4000 (the cohort present at the archive's start), joined to the actions
table — the query the fix-pass should run in full:

```sql
WITH big AS (
  SELECT DISTINCT security_id FROM (
    SELECT d.security_id, d.trade_date, d.adj_close,
           lag(d.adj_close)  OVER w prev,
           lag(d.trade_date) OVER w prev_d,
           lag(d.close)      OVER w prev_raw
    FROM price_bars_daily d
    WHERE d.security_id BETWEEN 1 AND 4000
    WINDOW w AS (PARTITION BY d.security_id ORDER BY d.trade_date)) x
  WHERE prev IS NOT NULL AND prev > 0 AND (trade_date - prev_d) <= 5 AND prev_raw >= 1
    AND (adj_close/prev < 0.6 OR adj_close/prev > 1.6667))
SELECT (SELECT count(*) FROM big)                                                     secs_with_big_gap,
       (SELECT count(*) FROM big b WHERE NOT EXISTS (
          SELECT 1 FROM corporate_actions ca
          WHERE ca.security_id = b.security_id AND ca.action_type = 'split'))          no_split_recorded;
-- 488 | 346      (71% of flagged securities have NO split on record)
```

Pro-rated to the 2,347 flagged securities: ≈1,660 with a large post-adjustment gap and no split. Some
of those are genuine (biotech readouts, meme squeezes, earnings collapses) and some are the ~3,300
candidates yfinance could not resolve because they are delisted — but the point is that the archive
cannot currently distinguish the two, and `verify`'s round-ratio-plus-persistence filter deliberately
reports only the subset that *looks* like a 2/3/4/10-for-1.

**Is coverage good enough?** For a **curated watchlist** (liquid, currently-listed, ≥\$5, provider
resolves cleanly): yes — the split math is verified exact against an independent provider for every
large-cap I tested. For a **universe backtest**: no. `DATA_INVENTORY.md`'s "the price side is now
genuinely sufficient" should be qualified to name the universe it is sufficient for.

### 8. The benchmark validation claim — and a sharper test

The number checks out to 3 basis points:

```
our SPY close 2020-10-02 = 333.87, 2025-10-02 = 669.17  ->  +100.428%
official (yfinance raw)    333.84 ->  669.22            ->  +100.46%
```

**But the test validates almost nothing.** Two endpoint prices, out of 1,241 sessions, agreeing on a
100% move. Its sensitivity:

```sql
WITH spy AS (SELECT d.trade_date, d.adj_close, row_number() OVER (ORDER BY d.trade_date) rn,
                    count(*) OVER () n
             FROM price_bars_daily d JOIN securities s ON s.id=d.security_id WHERE s.symbol='SPY')
SELECT 'true'      , round((((SELECT adj_close FROM spy WHERE rn=(SELECT n FROM spy LIMIT 1))
                            /(SELECT adj_close FROM spy WHERE rn=1))-1)*100,3)
UNION ALL SELECT '+1 session' , round((((SELECT adj_close FROM spy WHERE rn=(SELECT n FROM spy LIMIT 1))
                            /(SELECT adj_close FROM spy WHERE rn=2))-1)*100,3)
UNION ALL SELECT '+5 sessions', round((((SELECT adj_close FROM spy WHERE rn=(SELECT n FROM spy LIMIT 1))
                            /(SELECT adj_close FROM spy WHERE rn=6))-1)*100,3)
UNION ALL SELECT '+20 sessions', round((((SELECT adj_close FROM spy WHERE rn=(SELECT n FROM spy LIMIT 1))
                            /(SELECT adj_close FROM spy WHERE rn=21))-1)*100,3);

-- true         100.428
-- +1 session    96.971
-- +5 sessions   92.933
-- +20 sessions 104.934
```

A **one-month** misalignment still lands on 104.9%, comfortably inside any tolerance you would set on
"about 100%". A consistent one-session offset lands on 97.0%. The test cannot detect either. It also
cannot detect: the B-S4 close error (it moved the total by 3 bps), the dividend gap (that is 14.6 pp,
which *would* show — and the reported 100.4% is in fact the price-only number, so the "validation"
quietly confirms B-S2 rather than the pipeline), a systematic volume error, or anything about the
9,000 non-SPY names.

**Proposed sharper test** — a per-session, multi-symbol reconciliation, run in CI against a small
pinned fixture and on demand against the provider:

1. **Date alignment, exactly.** For each of ~20 continuously-listed reference symbols across sectors
   and exchanges, assert the set of session dates in `price_bars_daily` **equals** the set from
   `market_calendar WHERE is_trading_day` over the window, minus the explicitly-registered gap dates.
   A one-session offset fails immediately, and it is impossible to pass by luck.
2. **Per-session level bound.** Assert `max |bps|` on the close ≤ a threshold *chosen from the
   agreed convention*: ~1 bps if the close moves to the official auction print, and an explicit
   documented number (today's SPY worst case is 95.7 bps) if it stays at 15:59. Assert the **mean
   signed** difference is within ±0.5 bps, which is what actually catches a systematic error.
3. **Paired daily-return distribution.** Assert `sd(our_ret − their_ret) ≤ 1 bps/day` and
   `corr ≥ 0.9999`. Today SPY is 4.48 bps/day and MSFT 6.13 — this is the metric that a level bound
   alone lets through, and it maps directly onto the Sharpe error.
4. **Realised-vol agreement.** Assert annualised vol within ±0.5% relative (today SPY ours 17.082% vs
   official 17.331%, −1.44%). Vol is what the denominator of every ratio is made of.
5. **Split-adjustment cross-check with a PIT cutoff.** For NVDA/AMZN/GOOGL/TSLA/WMT, assert
   `split_adj_factor` equals the product of splits with `ex_date` in `(bar_date, window_end]` —
   note the **closed upper bound**, which is the B-S1 fix, and which yfinance's current-basis series
   will then *fail to match* by construction. That failure is the correct outcome and is the test that
   proves the lookahead is gone.
6. **Volume.** Assert median ours/official within a documented band, so a silent change in what the
   session window includes is caught.

Point 5 deserves emphasis: today `adj_close` **agrees** with yfinance's current-basis adjusted close to
−0.024 bps on NVDA. That agreement is not evidence of correctness — it is *proof* that our series is
computed on the 2026 share basis rather than the as-of-window basis, which is exactly B-S1.

### 9. Post-window split contamination — the demonstrating queries

```sql
SELECT (SELECT max(trade_date) FROM price_bars_daily)                                  archive_end,
       (SELECT count(*) FROM corporate_actions
          WHERE action_type='split' AND ex_date > (SELECT max(trade_date) FROM price_bars_daily)) splits_after,
       (SELECT count(DISTINCT security_id) FROM corporate_actions
          WHERE action_type='split' AND ex_date > (SELECT max(trade_date) FROM price_bars_daily)) secs_affected,
       (SELECT count(*) FROM corporate_actions WHERE announced_at IS NOT NULL)          with_announced_at,
       (SELECT count(*) FROM corporate_actions)                                          actions_total;
-- 2025-10-02 | 527 | 436 | 0 | 46934

-- bars whose factor/level was computed from a split nobody could have known about
WITH fut AS (SELECT security_id FROM corporate_actions
             WHERE action_type='split' AND ex_date > '2025-10-02' GROUP BY security_id)
SELECT count(*) FROM price_bars_daily d JOIN fut f ON f.security_id = d.security_id;
-- 308709

-- THE DEMONSTRATION: a $5 price floor applied to adj_close admits names that fail it on close
WITH fut AS (SELECT security_id, min(ex_date) first_future_ex FROM corporate_actions
             WHERE action_type='split' AND ex_date > '2025-10-02' GROUP BY security_id)
SELECT count(*) bars_wrongly_passing, count(DISTINCT d.security_id) securities
FROM price_bars_daily d JOIN fut f ON f.security_id = d.security_id
WHERE d.close < 5 AND d.adj_close >= 5;
-- 196909 | 357

-- worked rows, one session
-- symbol | trade_date | raw_close | adj_close   | split_adj_factor | first_future_ex
-- PAVS   | 2025-09-30 |    1.0400 | 124800.0050 | 0.000008333333   | 2025-12-18
-- WOK    | 2025-09-30 |    0.0748 |  74800.0000 | 0.000001000000   | 2025-10-21
-- AREB   | 2025-09-30 |    0.9600 |  38400.0000 | 0.000025000000   | 2025-10-03
-- HUBC   | 2025-09-30 |    1.9800 |  29699.9985 | 0.000066666670   | 2026-01-16
-- OCG    | 2025-09-30 |    3.7000 |   2442.0025 | 0.001515149985   | 2026-01-16
-- VSA    | 2025-09-30 |    3.4400 |   1720.0000 | 0.002000000000   | 2025-12-22

-- and the levels this produces at the far end of the archive
SELECT s.symbol, max(d.adj_close) max_adj_close, min(d.split_adj_factor) min_factor
FROM price_bars_daily d JOIN securities s ON s.id = d.security_id
WHERE d.security_id IN (SELECT security_id FROM corporate_actions
                        WHERE action_type='split' GROUP BY security_id HAVING count(*) >= 6)
GROUP BY 1 ORDER BY 2 DESC LIMIT 4;
-- ADTX | 2630000000000.0000000000 | 0.000000000002
-- WHLR |   58260869565.2173913043 | 0.000000000092
-- EJH  |    6645000000.0000000000 | 0.000000008000
-- XXII |    1248602727.4759669126 | 0.000000004473
```

`adj_close IS NULL` count across all 12,840,439 bars: **0**. 006's fail-safe never fires.

Confirming the basis is 2026 rather than 2025-10-02, from the provider:

```
WHLR  last_bar=2025-10-02  factors 9.2e-11 .. 0.00278   16 splits, 5 with ex_date AFTER the last bar
                                    (2025-12-01 @0.5, 2026-01-20 @1/3, 2026-04-20 @1/3, 2026-06-18 @0.25, …)
PAVS  last_bar=2025-10-02  4 splits, 3 after (2025-12-18 @0.01, 2026-03-31 @1/12, 2026-06-29 @0.01)
AREB  last_bar=2025-10-02  7 splits, 3 after
NVDA  last_bar=2025-10-02  6 splits, 0 after   <- unaffected, which is why the NVDA validation passes
```

**Fix.** `split_factor_after` needs a point-in-time upper bound, and there are two defensible designs:

- **`split_factor_between(security_id, bar_date, as_of_date)`** — `ex_date > bar_date AND ex_date <=
  as_of_date`. The correct general answer; makes the adjustment a function of the decision time, which
  is what §3.5 asks for, at the cost of not being materialisable into a single column.
- **A materialised factor pinned to a declared `adjustment_as_of`** stored on the table or in
  `data_sources`, plus a hard rule that any split with `ex_date > adjustment_as_of` is refused by the
  adjust pass. Cheaper, keeps the column, and makes the contamination window explicit and auditable.

Either way: **do not re-run `adjust` before this decision is made**, or the lookahead is simply
re-baked into 12.8M rows. And add the `announced_at` filter 005 already promises — for splits it is
usually available from a real corporate-actions feed and is the thing that makes the PIT claim real
rather than asserted.

---

## What can still produce a plausible wrong number

Ranked by how plausible the wrong number looks. The top four are the ones that will not announce
themselves.

1. **`adj_close` used as a marking or screening price** (B-S1, B-S3). $\Sigma$ `shares × adj_close`
   with as-traded share counts is wrong by the split factor — 40× for a pre-2021 NVDA lot — and the
   result is a *plausible* market value, not an obviously broken one. A \$5 price floor on `adj_close`
   admits 357 penny stocks on the strength of splits announced after the decision date. Neither
   throws.
2. **A price-only return stored in a column named `total_return`** (B-S2). Nothing in the schema can
   express the difference, the magnitude (1.6–9.1 pp/yr) is exactly the size of a plausible alpha, and
   it is correlated with the strategy under test.
3. **A Sharpe whose rf conversion nobody wrote down** (S-S2, S-S3). 0.7747 or 0.8301 for the same
   returns, and both are stored with identical `risk_free_annual` and `periods_per_year`. Add the
   discount-vs-investment basis error and every one of them is ~0.01 too generous.
4. **A 16-session move recorded as one daily return** (S-S1). −3.321% for SPY on 2025-01-02 looks like
   an ordinary bad day. It costs 5.4% of Sharpe on the benchmark and it passes both n-checks.
5. **A ticker-reuse series producing a +255% or −66% single-session return** (B-S5). FLY, FLXN, PCI,
   TOT, GLIBA and 278 others. Large enough to dominate a max-drawdown or hit-rate statistic, and
   indistinguishable from a real event without external reference data.
6. **A position in a delisted name that never dies** (B-S5). If the marking job forward-fills, the
   position holds its last price forever and the book never realises the loss — the single most
   return-inflating bug in this class, and 7,621 securities are eligible.
7. **A close that is 96 bps off on the day it matters most** (B-S4). 2025-04-09 was one of the largest
   single-session moves in the window; that is the day a strategy's return is decided, and it is the
   day our close is worst.
8. **A liquidity or slippage model built on volume that is 15% low** (B-S4). Median ours/official
   0.853 for SPY. A participation cap calibrated on this will size 15% too small — or an ADV filter
   will exclude names that are in fact tradeable.
9. **Returns computed off `close` because `split_adj_factor` was NULL** (S-S8). No `NOT NULL`, no
   trigger; incremental bars arrive unadjusted, and NVDA's raw series carries 74.6% annualised vol
   against a true 52.4%.
10. **A "universe" containing warrants, rights, units and preferreds** (S-S7). 137 + 88 + 680 symbols
    with no `security_type` to filter on. These are also where the split-adjustment residue and the
    price-floor contamination concentrate.
11. **`first_seen` read as a listing date** (S-S7). 8,678 securities carry 2020-10-02 — the archive's
    first day, not theirs. An "IPO within N days" or "seasoned issue" feature built on it is uniformly
    wrong for 45% of the universe.
12. **An `evaluation_runs` row whose `n_observations` is honest and whose window is not** (S-S1). Both
    constraints pass; the annualisation is scaled by 252 over a series with holes.

---

## Coordination observations

- **`REVIEW_phaseA_loaders.md` S-6 is adjudicated here as B-S4.** That review correctly identified the
  09:30–15:59 / early-close inconsistency, correctly found it "empirically benign" for the 14 half
  days, and explicitly deferred the domain ruling. The ruling is: the half-day half is fine (better
  than fine — Polygon emits nothing 13:01–15:59, so half-days capture the auction window), and the
  full-day half is a blocker whose fix is a **data-source change**, not a constant change. Please do
  not resolve S-6 by moving `SESSION_LAST_MINUTE` to 16:00 — the 16:00 bucket's *close* is a
  post-close print (SPY 2025-04-09: 544.30 against an official 548.62), so that change trades one
  wrong number for a different wrong number.
- **The peer review's B-1 is the mechanism behind my B-S2 and S-S5's magnitude.** Fix order matters:
  (1) B-1 so provider failures are loud; (2) re-fetch with `--candidates all` so dividends and small
  splits exist for the whole universe; (3) resolve B-S1's `ex_date` cutoff; (4) only then re-run
  `adjust`. Running `adjust` before step 3 re-bakes the lookahead into 12.8M rows.
- **The peer review's N-6 and my S-S9 are the same defect at two levels** — it flagged the comment as
  misleading about `NUMERIC(30,10)`; I am flagging that the consequence is 006's documented
  fail-loud contract being unreachable and 0 NULLs in practice. Fix both together: correct the
  comment *and* lower the guard (or, better, stop materialising a level whose magnitude is
  meaningless).
- **The peer review's S-5** (`ON CONFLICT DO NOTHING` discarding a differing action) intersects
  S-S4: the same idiom is what discards revised FRED rates. One fix pattern — detect
  `rowcount == 0` with a value mismatch and warn — covers both tables.
- **For the schema reviewer:** three of my findings want new columns rather than new code —
  `evaluation_runs.return_basis` ('price_only' | 'total'), an rf-conversion field or a documented
  convention constant, and either `expected_n`/`coverage_ratio` on `evaluation_runs` or a trigger
  joining it to `market_calendar`. Worth deciding together so it is one migration, not three.
- **For whoever writes the marking job:** it is currently unspecified for the two cases that will
  occur on day one — a security with no bar on a session (delisted, halted, or inside the December
  hole) and a security with no dividend coverage. Both must fail loudly per Bar §7.2 "fail to SAFE",
  not forward-fill.
- **`DATA_INVENTORY.md`'s closing verdict** ("the price side is now genuinely sufficient") predates
  the split/dividend load and should be re-scoped: sufficient for a curated watchlist, not for a
  universe backtest, and the fundamentals-vs-price framing it uses is exactly the right shape for
  saying so.
- **Nothing consumes any of this yet** — `grep` for `adj_close|sharpe|risk_free` across `backend/`,
  `src/` and `frontend/` returns only the loaders, the migrations and the runner tests. That is the
  good news: every finding here is cheap to fix *now* and, per §3.5, "effectively impossible to
  retrofit" later.

---

**Verification hygiene:** all queries above were run read-only against `rh-db`; the two long-running
window scans I started were terminated with `pg_cancel_backend`; `pg_stat_activity` shows no active
statements from this review; `docker inspect rh-db` → `running healthy`. No rows were inserted,
updated or deleted in `price_bars_daily`, `corporate_actions`, `market_calendar`, `risk_free_rates` or
`securities`. Four temporary audit scripts were written under `db/` and deleted; `git status` is clean.
No `km-*` object was touched.
