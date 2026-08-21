"""Composite scoring across the direction models, and the disagreement signal that falls out of it.

PORTED from Special-Sprinkle-Sauce `src/intelligence/quant_models/orchestrator.py`, with four
deliberate departures:

1. NO MOCK MODE. The original took `use_mock=True` as its DEFAULT and returned canned scores from
   `mock_scores.get_mock_scores()`. A composite that silently averages fabricated numbers is the
   defect this repo keeps finding: a value whose name says "score" and whose content is fiction.
   Only real predictions enter the average here.

2. NO 0.5 FALLBACK. The original substituted 0.5 for any model it could not run — no fundamentals,
   too few bars, a missing library. 0.5 is not "unknown", it is "a coin flip, confidently", and it
   drags the composite toward neutral while *shrinking* the standard deviation, so the very cases
   with the least information look like the cases with the most model agreement. A model that
   cannot score is reported as scoring nothing, and both the composite and the dispersion are taken
   over the models that actually ran.

3. THE SENTIMENT MODEL IS NOT PORTED. It requires Finnhub and NewsAPI credentials this app does not
   hold, and its own no-key path returns mock scores. Four tree/linear/time-series models remain,
   which is what the Lab compares. Add it back only alongside real credentials.

4. RANDOM FOREST JOINS THE PANEL. See random_forest_model.py.

Threshold inlined from SSS `app.services.risk.constants.HIGH_MODEL_DISAGREEMENT_THRESHOLD` — that
module is a FastAPI-layer import and this package stays framework-agnostic.
"""

from __future__ import annotations

import logging
import statistics

from .arima_model import ARIMAModel
from .elastic_net_model import ElasticNetDirectionModel
from .errors import UntrainedModelError
from .random_forest_model import RandomForestDirectionModel
from .xgboost_model import XGBoostDirectionModel

logger = logging.getLogger("agentic.ml.orchestrator")

# Dispersion above which the panel is treated as disagreeing rather than merely differing.
# Inlined from SSS app/services/risk/constants.py:27 (HIGH_MODEL_DISAGREEMENT_THRESHOLD = 0.50).
HIGH_MODEL_DISAGREEMENT_THRESHOLD = 0.50

# ARIMA needs a real history before it can say anything; below this it declines rather than guesses.
_MIN_ARIMA_OBSERVATIONS = 30


class QuantModelOrchestrator:
    """Runs every available direction model over a ticker and reports the panel's spread.

    The dispersion matters more than the composite. Four models trained on the same features
    agreeing tells you little; four models disagreeing tells you the setup is genuinely ambiguous,
    which is a reason to size down or stay out. That signal only survives if unavailable models
    abstain instead of voting 0.5.
    """

    def __init__(self) -> None:
        self._xgboost = XGBoostDirectionModel()
        self._random_forest = RandomForestDirectionModel()
        self._elastic_net = ElasticNetDirectionModel()
        self._arima = ARIMAModel()

    # -- scoring ---------------------------------------------------------------------------------

    def score_ticker(
        self,
        ticker: str,
        ohlcv_df=None,
        fundamentals: dict | None = None,
    ) -> dict:
        """Score one ticker across the panel.

        Returns `scores` (model -> P(up), only for models that ran), `abstentions`
        (model -> why it did not), and the composite/dispersion taken over `scores` alone.
        `composite` and `std_dev` are None when too few models ran to define them — None because
        there is no number that honestly stands in for "we did not measure this".
        """
        ticker = ticker.upper()
        scores: dict[str, float] = {}
        abstentions: dict[str, str] = {}

        for name, model, payload, need in (
            ("xgboost", self._xgboost, fundamentals, "fundamentals"),
            ("random_forest", self._random_forest, fundamentals, "fundamentals"),
            ("elastic_net", self._elastic_net, fundamentals, "fundamentals"),
        ):
            if not payload:
                abstentions[name] = f"no {need} supplied"
                continue
            self._record(name, lambda m=model, p=payload: m.predict(p), scores, abstentions)

        closes = self._closes(ohlcv_df)
        if closes is None:
            abstentions["arima"] = "no OHLCV history supplied"
        elif len(closes) < _MIN_ARIMA_OBSERVATIONS:
            abstentions["arima"] = (
                f"only {len(closes)} observations, needs {_MIN_ARIMA_OBSERVATIONS}"
            )
        else:
            self._record("arima", lambda: self._arima.predict(closes), scores, abstentions)

        values = list(scores.values())
        composite = round(statistics.mean(values), 4) if values else None
        std_dev = round(statistics.stdev(values), 4) if len(values) > 1 else None

        return {
            "ticker": ticker,
            "scores": {k: round(v, 4) for k, v in scores.items()},
            "abstentions": abstentions,
            "models_scored": len(values),
            "composite": composite,
            "std_dev": std_dev,
            # None, not False: with fewer than two live scores there is no dispersion to judge, and
            # False would read as "the panel agreed" when the panel never convened.
            "high_disagreement_flag": (
                None if std_dev is None else std_dev > HIGH_MODEL_DISAGREEMENT_THRESHOLD
            ),
        }

    def score_multiple(
        self,
        tickers: list[str],
        ohlcv_data: dict | None = None,
        fundamentals_data: dict | None = None,
    ) -> dict[str, dict]:
        """Score each ticker independently. One ticker's abstentions never affect another's."""
        ohlcv_data = ohlcv_data or {}
        fundamentals_data = fundamentals_data or {}
        return {
            ticker: self.score_ticker(
                ticker,
                ohlcv_df=ohlcv_data.get(ticker),
                fundamentals=fundamentals_data.get(ticker),
            )
            for ticker in tickers
        }

    # -- reporting -------------------------------------------------------------------------------

    def get_all_manifests(self) -> dict:
        """Every model's manifest, including whether it is trained at all."""
        return {
            "xgboost": self._xgboost.get_manifest(),
            "random_forest": self._random_forest.get_manifest(),
            "elastic_net": self._elastic_net.get_manifest(),
            "arima": self._arima.get_manifest(),
        }

    def get_agreement_metrics(
        self,
        tickers: list[str],
        ohlcv_data: dict | None = None,
        fundamentals_data: dict | None = None,
    ) -> dict:
        """Dispersion statistics across tickers.

        `tickers` is required — the original defaulted to a hardcoded PILOT_TICKERS list that lived
        in the mock module, so "agreement metrics" could be computed over tickers nobody asked for.

        Tickers where fewer than two models scored are excluded and counted in `tickers_unscored`,
        so a run that mostly abstained cannot pass itself off as a run that mostly agreed.
        """
        all_scores = self.score_multiple(tickers, ohlcv_data, fundamentals_data)
        scored = {t: s for t, s in all_scores.items() if s["std_dev"] is not None}
        std_devs = [s["std_dev"] for s in scored.values()]

        return {
            "tickers_requested": len(tickers),
            "tickers_scored": len(scored),
            "tickers_unscored": len(all_scores) - len(scored),
            "avg_std_dev": round(statistics.mean(std_devs), 4) if std_devs else None,
            "max_std_dev": round(max(std_devs), 4) if std_devs else None,
            "min_std_dev": round(min(std_devs), 4) if std_devs else None,
            "high_disagreement_count": sum(
                1 for s in scored.values() if s["high_disagreement_flag"]
            ),
            "high_disagreement_tickers": [
                t for t, s in scored.items() if s["high_disagreement_flag"]
            ],
            "threshold": HIGH_MODEL_DISAGREEMENT_THRESHOLD,
        }

    # -- internals -------------------------------------------------------------------------------

    @staticmethod
    def _record(name, call, scores: dict, abstentions: dict) -> None:
        """Run one model's predict(), recording either its score or why it abstained.

        An untrained model is an expected state in the Lab (the panel is assembled before every
        member is fitted), so it abstains rather than aborting the whole panel. Anything else is
        logged at ERROR — an abstention must never be a quiet way to lose a real failure.
        """
        try:
            scores[name] = float(call())
        except UntrainedModelError as e:
            # First sentence only — the rest of the message explains WHY there is no placeholder,
            # which belongs in the log, not in a field the UI renders next to a ticker.
            abstentions[name] = str(e).split(". ")[0]
            logger.debug("%s abstained: %s", name, e)
        except Exception as e:  # noqa: BLE001 — one model's failure must not void the whole panel
            abstentions[name] = f"prediction failed: {type(e).__name__}"
            logger.error("%s prediction failed: %s", name, e)

    @staticmethod
    def _closes(ohlcv_df):
        """Pull the close series out of whatever OHLCV shape was handed in, or None."""
        if ohlcv_df is None:
            return None
        try:
            if hasattr(ohlcv_df, "columns"):
                return ohlcv_df["close"].values
            return ohlcv_df
        except Exception as e:  # noqa: BLE001 — an unusable frame is an abstention, not a crash
            logger.error("could not read close series: %s", e)
            return None
