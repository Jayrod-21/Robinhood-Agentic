# Data inventory — `data/market/`

What's in the local market-data archive, where it came from, and what it can and can't support.

`data/market/` is **gitignored** — 23 GB of licensed third-party exports has no business in git. This
document is the tracked record of what should be there, so a fresh clone knows what's missing.

**Sources — two drives, copied on two days:**

| Source | Contents | Copied | Verified |
|---|---|---|---|
| `T-Drive` (233 GB, exfat) | Bloomberg files, Wasden PDFs, and an 11-month minute-bar subset | 2026-07-27 | 262 files / 4,497,840,236 bytes, exact match |
| `Extreme SSD` (931 GB, exfat) → `4. Stock Data/Emery 5 Year` | **the full 5-year minute-bar archive** | 2026-07-28 | 1,256 files / 24,447,484,929 bytes, exact match |

Both verified by file count, byte total, and a dry-run `rsync` reporting nothing left to transfer.

**The T-Drive minute-bar subset has been deleted** (2026-07-28, reclaiming 4.2 GB). It was a strict
subset of the 5-year set: all 229 files present in the new archive with identical sizes, and a
20-file sha256 sample across the date range matched exactly. Verified before deletion, not assumed.

To restore on another machine, re-mount the drives and:

```bash
rsync -a --info=progress2 --exclude="System Volume Information" \
  /run/media/<user>/T-Drive/ "data/market/"
rsync -a --info=progress2 \
  "/run/media/<user>/Extreme SSD/4. Stock Data/Emery 5 Year/" "data/market/minute_bars_5y/"
```

---

## 1. Minute bars — `minute_bars_5y/`

**24 GB · 1,256 gzipped daily CSVs · `YYYY/MM/YYYY-MM-DD.csv.gz`**

Polygon.io flat-file minute-aggregate format. Columns:

```
ticker, volume, open, close, high, low, window_start, transactions
```

`window_start` is a **nanosecond** epoch (e.g. `1605191340000000000`). A sampled day file
(`2020-11-12`) holds **1,435,677 rows** — this is the full US equity universe, not a watchlist. Row
count across the archive is estimated in "Coverage" below.

**Timestamps cross month boundaries.** Polygon day files carry post-market bars through 20:00 ET, so
during EST (UTC−5) the late bars of the last trading day of a month have UTC timestamps in the *next*
month. The loader must therefore create partitions for the file's actual `min(ts)..max(ts)`, not for
its nominal date — see `ensure_price_bar_partitions()` in migration 002. Getting this wrong wedges
ingest at the first EST month-end, which is 2020-11-30, about two months into a full load.

### Coverage

**2020-10-02 → 2025-10-02 — five years, 1,256 trading days.** Per year: 63 (2020, from October) ·
252 (2021) · 251 (2022) · 250 (2023) · 252 (2024) · 188 (2025, through October).

At ~1.44M rows per file this is on the order of **1.5–1.8 billion rows** — which is what drove the
monthly RANGE partitioning in migration 002, and why `price_bars_minute` carries no per-row audit
columns (two `timestamptz` columns would cost ~26 GB for information already recorded once per file
in `data_sources`).

This window spans genuinely different regimes — the post-COVID recovery, the 2021 meme-stock period,
the 2022 bear market, and the 2023-2025 recovery — which is what makes walk-forward evaluation
meaningful rather than a single-regime artifact. It is still only five years: enough to evaluate a
strategy honestly, not enough to claim an edge survives every market. The 30-year history in FMP's
Premium tier is the complement, and it is daily rather than minute resolution.

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
| Intraday microstructure features (minute bars) | **Yes** — §1, five years, full universe |
| Backtest across multiple market regimes | **Yes** — 2020-2025 covers a recovery, a mania, a bear market, and a recovery |
| Point-in-time *fundamentals* history | **No** — §2 is a 4-day snapshot. This is the real gap, and it is what FMP Premium's quarterly + 30-year history fills |
| Train and evaluate an ML model | **Yes for the pipeline; qualified for the conclusion** — five years is enough to evaluate walk-forward honestly, not enough to claim an edge survives every market |

The honest read: the **price** side is now genuinely sufficient — five years of full-universe minute
bars across four distinct regimes will support a real backtest and a real evaluation harness.

The **fundamentals** side is not. Four days of Bloomberg is a validation sample, not a history, so a
fundamentals-driven strategy cannot yet be backtested at all — the screen can be checked for
correctness against real values, but not evaluated over time. That gap is the entire argument for FMP
Premium's quarterly fundamentals and 30-year depth, and it is worth being precise that these are two
separate readiness states rather than one blended "we have data".
