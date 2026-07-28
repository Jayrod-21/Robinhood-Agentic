# Data inventory — `data/market/`

What's in the local market-data archive, where it came from, and what it can and can't support.

`data/market/` is **gitignored** — 4.5 GB of licensed third-party exports has no business in git. This
document is the tracked record of what should be there, so a fresh clone knows what's missing.

**Source:** the `T-Drive` external disk (233 GB, exfat), mounted at
`/run/media/jared-williams/T-Drive`. Copied 2026-07-27 with `rsync -a`, excluding
`System Volume Information`.

**Verified after copy:** 262 files, 4,497,840,236 bytes — file count and byte total both match the
source exactly, and a dry-run `rsync` finds nothing left to transfer.

To restore on another machine, re-mount the drive and:

```bash
rsync -a --info=progress2 --exclude="System Volume Information" \
  /run/media/<user>/T-Drive/ "data/market/"
```

---

## 1. Minute bars — `2. Stock Data/Stocks_min_min_5_year/`

**4.2 GB · 229 gzipped daily CSVs · `YYYY/MM/YYYY-MM-DD.csv.gz`**

Polygon.io flat-file minute-aggregate format. Columns:

```
ticker, volume, open, close, high, low, window_start, transactions
```

`window_start` is a **nanosecond** epoch (e.g. `1605191340000000000`). A sampled day file
(`2020-11-12`) holds **1,435,677 rows** — this is the full US equity universe, not a watchlist — so
the archive is on the order of **300 million rows**.

### ⚠ The folder name is wrong, and it matters

Despite `5_year`, the actual coverage is **2020-10-02 → 2021-08-30 — about eleven months**, 229
trading days (63 files in 2020, 166 in 2021). Two consequences worth deciding on before building
anything on top of it:

- **It is a short history for ML.** 229 days of daily-resolution signal is thin, even though the
  minute resolution makes the row count look enormous. Row count is not the same as independent
  observations.
- **It is one specific regime** — the post-COVID-crash recovery and the early-2021 meme-stock period.
  A model fit only on this window learns that regime's behavior. Any evaluation must be walk-forward
  within it, and results should not be read as a general-market edge.

If a longer history matters, that's a reason to buy the paid FMP tier (or a Polygon subscription) and
backfill, rather than to squeeze more out of these 11 months.

## 2. Bloomberg fundamentals — `2_21_2026_thru_2_24_2026.csv`

**12 KB · 4 days (2026-02-21 → 2026-02-24), tickers in Bloomberg form (`NVDA US Equity`)**

Wide fundamentals snapshot, and notably it carries **exactly the Sprinkle Sauce screen inputs**:
P/E trailing and forward, PEG, Free Cash Flow, FCF Yield, EBITDA margin, ROE, ROC, gross/operating/net
margin, current and quick ratio, debt/equity, revenue growth YoY, EBITDA/interest, **Piotroski
F-Score**, cash conversion cycle, and short interest.

**Ingest must handle Excel error strings, not just nulls.** Real values in this file include
`#N/A Invalid Field`, `#VALUE!`, and `#N/A N/A`, plus scientific-notation market caps
(`4.61263E+12`). A naive `float()` cast will throw or, worse, silently coerce. These become `NULL`
with the original string preserved for provenance.

Four days is a sample, not a history — useful for validating the ingest and the screen against real
Bloomberg values, not for backtesting.

## 3. `dowjones1 data.xlsx`

**1.1 MB.** Contents not yet characterized. Pairs with the `DowLarger1a` / `DowSmall1a` architectures
in 3a's `models/miller_nn/`, so it is likely the training set for those. Worth opening before the ML
work starts.

## 4. `2. Stock Data/Bloomberg/`

- `JMWFM_Bloomberg_Data_Pulling.xlsx` (31 KB) — the Bloomberg pull sheet; presumably the formulas
  that generated §2.
- `fc12404b-184c-455a-ad3d-76c51a2112f2` (2.9 KB) — **not data.** `file` reports a 96×96 PNG icon.
  Ignore it.

## 5. `wasden_corpus/`

**8.7 MB · 29 Wasden Weekender PDFs**, 2022-06-12 onward. These duplicate
`3a. SpecialSprinkleSauce/data/wasden_corpus/`. Keep one canonical copy; this one is the archive.

---

## What this supports, and what it doesn't

| Goal | Supported? |
|---|---|
| Validate the screen against real Bloomberg fundamentals | **Yes** — §2, for 4 days |
| Intraday microstructure features (minute bars) | **Yes** — §1, for 11 months |
| Daily-bar backtest over a meaningful history | **No** — 11 months, one regime. Needs a backfill. |
| Point-in-time fundamentals history | **No** — §2 is a 4-day snapshot, not a time series |
| Train and evaluate an ML model | **Partly** — enough to build and test the pipeline end to end; not enough to trust an edge estimate |

The honest read: this is **enough to build and validate the whole infrastructure** — schema, ingest,
feature store, evaluation harness, training loop — against real data rather than synthetic fixtures.
It is not enough to conclude a strategy works. Those are different milestones and should not be
conflated.
