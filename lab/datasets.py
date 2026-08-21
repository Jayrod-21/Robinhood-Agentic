"""Where the Lab's training data comes from, and what it refuses to pretend about.

TWO SOURCES, NEVER A FALLBACK BETWEEN THEM
    `synthetic` generates bars from a seed; `historical_bars` reads real daily bars out of
    price_bars_daily. If the real source has nothing to give, this module RAISES. It does not quietly
    return synthetic data instead.

    That is not a hypothetical concern. Joe's own note on the repo this was ported from says it
    outright: "the sweep runner's non-mock code paths are explicit stubs that silently return mock
    data, and the backtest router falls back to mock even when use_mock_data=False." A Lab that
    answers a request for real data with generated data produces a leaderboard that is worse than
    empty, because it looks full.

THE LABEL BUG THIS MODULE ROUTES AROUND
    FeatureEngineer.build_labels computes `close.shift(-forward_days) / close - 1 > 0`. For the last
    `forward_days` rows the forward return is NaN, `NaN > 0` is False, and `.astype(int)` turns that
    into a label of 0 — so the most recent week of every series is silently labelled "down" whether
    it went down or not. Unknown rendered as a confident value, the same defect the model wrappers
    carried. `features_and_labels` below computes the forward return itself and DROPS the rows whose
    label is not yet knowable, which is the only honest thing to do with them.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("lab.datasets")

# Below this there is not enough history for the rolling windows (a 50-day SMA plus a 20-day
# volatility) to leave a usable sample behind, let alone for walk-forward to have steps to take.
MIN_BARS = 250


class DatasetUnavailable(RuntimeError):
    """The requested data does not exist. Raised instead of substituting generated data."""


def synthetic(seed: int = 42, n_bars: int = 750) -> pd.DataFrame:
    """A geometric random walk with realistic-looking OHLCV. Honest about being generated.

    Named `synthetic`, recorded as `data_source='synthetic'`, and stored in a column with no
    default — so a model validated on this can never be ranked as though it saw a real market.
    """
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0004, 0.016, n_bars)
    close = 100.0 * np.exp(np.cumsum(returns))
    # Intraday range around the close, then open/close forced inside [low, high] so the frame obeys
    # the same OHLC invariant the price_bars_daily CHECK constraints enforce on real rows.
    spread = np.abs(rng.normal(0, 0.008, n_bars)) * close
    high = close + spread
    low = close - spread
    open_ = np.clip(close * (1 + rng.normal(0, 0.006, n_bars)), low, high)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2020-01-01", periods=n_bars),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(500_000, 5_000_000, n_bars),
        }
    )


def historical_bars(conn, symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Real daily bars for one symbol, or a refusal.

    Uses adj_close when the provider supplied one, and the raw close otherwise — returns must be
    computed from split- and dividend-adjusted prices, and price_bars_daily keeps both side by side
    precisely because they are not interchangeable. The other three legs are left raw, since they
    only feed range and volume features that are scale-free within a bar.
    """
    rows = conn.execute(
        """
        SELECT b.trade_date, b.open, b.high, b.low,
               COALESCE(b.adj_close, b.close) AS close, b.volume
          FROM price_bars_daily b
          JOIN securities s ON s.id = b.security_id
         WHERE s.symbol = %s
           AND (%s::date IS NULL OR b.trade_date >= %s::date)
           AND (%s::date IS NULL OR b.trade_date <= %s::date)
         ORDER BY b.trade_date
        """,
        (symbol.upper(), start, start, end, end),
    ).fetchall()

    if not rows:
        raise DatasetUnavailable(
            f"no daily bars for {symbol.upper()} in {start or 'the beginning'}..{end or 'now'}. "
            "Refusing rather than substituting synthetic data — a leaderboard built on generated "
            "bars is worse than an empty one, because it looks full."
        )
    if len(rows) < MIN_BARS:
        raise DatasetUnavailable(
            f"{symbol.upper()} has only {len(rows)} bars in range; needs at least {MIN_BARS} for "
            "the rolling windows and a walk-forward split to leave anything to measure."
        )

    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    # psycopg returns NUMERIC as Decimal; the whole feature pipeline is float arithmetic and mixing
    # the two raises rather than coercing on some pandas versions.
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def features_and_labels(
    ohlcv: pd.DataFrame, forward_days: int = 5
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Aligned features and labels, with the unknowable rows dropped rather than guessed.

    Two independent things go wrong if this is done naively, and both are silent:

      1. build_features() drops the leading rows consumed by its rolling windows, so its output is
         a SUBSET of the input. Zipping it against labels built from the full frame shifts every
         label about fifty rows out of alignment — a model that trains beautifully on noise.
      2. The last `forward_days` rows have no forward return yet. build_labels turns that NaN into
         a 0, labelling the most recent week "down" regardless of what happened.

    So the join is on `date`, and rows whose forward return is NaN are dropped.
    """
    from src.ml.feature_engineer import FeatureEngineer

    features = FeatureEngineer.build_features(ohlcv)
    if features.empty:
        raise DatasetUnavailable(
            f"feature construction left no rows from {len(ohlcv)} bars — the rolling windows "
            "consumed the whole series."
        )

    prices = ohlcv[["date", "close"]].sort_values("date").reset_index(drop=True)
    forward_return = prices["close"].shift(-forward_days) / prices["close"] - 1
    # `.gt(0)` on a NaN-bearing float column, NOT `> 0` then astype(int) — the point of this whole
    # function is that NaN must survive as NaN long enough to be dropped instead of becoming 0.
    labels = pd.DataFrame(
        {"date": prices["date"], "label": forward_return.gt(0).where(forward_return.notna())}
    )

    joined = features.merge(labels, on="date", how="inner").dropna(subset=["label"])
    if joined.empty:
        raise DatasetUnavailable("no rows survived aligning features to knowable labels")

    feature_names = [c for c in features.columns if c != "date"]
    X = joined[feature_names].to_numpy(dtype=float)
    y = joined["label"].to_numpy(dtype=int)
    logger.info(
        "dataset: %d bars -> %d features rows -> %d aligned, labelled rows (%d dropped as unknowable)",
        len(ohlcv),
        len(features),
        len(joined),
        len(features) - len(joined),
    )
    return X, y, feature_names
