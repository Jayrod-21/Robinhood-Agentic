"""Sprinkle Sauce fundamental screen — lean re-implementation.

Mirrors the tier logic in 3a's ``backend/app/services/screening_engine.py`` and the
``sprinkle_sauce_spec.md`` spec, but self-contained and fed from FMP (``src/fmp.py`` via
``src/data.py::fundamentals_from_fmp``). It said "free yfinance data" until that source was
removed; the gate thresholds are unchanged because FMP reports margins and growth as the same
fractions yfinance did — which was checked against a real payload, not assumed.

Only the *fundamental* tiers run here (Tier 1 liquidity, Tier 2 Sprinkle Sauce). Tiers 3-5
in the original design (quant models, Wasden RAG verdict, final ranking) are handled by the
in-session Claude agent applying the Wasden lens to whatever this screen surfaces — so this
module's job is to produce a clean, ranked candidate list with transparent pass/fail reasons.

All functions are pure: they take a ``fundamentals`` dict and return structured results, so
they are trivially unit-testable without any network access.

Convention notes (kept identical to 3a so the two stay legible together):
- FCF yield is stored as a PERCENTAGE: 3.0 means 3%, threshold is 3.0 (not 0.03).
- Piotroski uses proportional single-snapshot scoring: signals whose prior-period inputs are
  unavailable are dropped, and the threshold scales as ``score / available >= 5/9``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Tier 1: liquidity ----------------------------------------------------------------
MIN_MARKET_CAP = 5_000_000_000  # $5B

# --- Tier 2: Sprinkle Sauce -----------------------------------------------------------
MAX_PEG = 2.0
MIN_FCF_YIELD = 3.0  # percentage units (3.0 == 3%)
PIOTROSKI_RATIO = 5 / 9  # proportional pass threshold

# Universe note: the charter widens the universe beyond pure large-cap to include liquid,
# volatile mid/small-caps. MIN_MARKET_CAP here is the *original* Wasden large-cap floor; the
# runner may relax it per-scan via ``min_market_cap`` so we can reach the more aggressive names
# while still recording why a name passed or failed the classic liquidity gate.


@dataclass
class TierResult:
    """Outcome of a single screening tier for one ticker."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class ScreenResult:
    """Full screen outcome for one ticker, across all fundamental tiers."""

    ticker: str
    passed: bool
    failed_tier: str | None
    tiers: dict[str, TierResult]
    composite: float | None  # pre-Wasden ranking score for survivors; None if screened out

    @property
    def reasons(self) -> list[str]:
        """Flat list of every fail reason across tiers (for display)."""
        out: list[str] = []
        for name, tier in self.tiers.items():
            out.extend(f"[{name}] {r}" for r in tier.reasons)
        return out


def tier1_liquidity(fundamentals: dict, min_market_cap: float = MIN_MARKET_CAP) -> TierResult:
    """Tier 1 — liquidity floor by market cap."""
    market_cap = fundamentals.get("market_cap")
    metrics = {"market_cap": market_cap}
    reasons: list[str] = []

    if market_cap is None:
        reasons.append("market_cap unavailable")
    elif market_cap < min_market_cap:
        reasons.append(f"market_cap ${market_cap:,.0f} < ${min_market_cap:,.0f}")

    return TierResult(passed=not reasons, reasons=reasons, metrics=metrics)


def _piotroski_proportional(fundamentals: dict) -> tuple[float | None, dict]:
    """Compute a proportional Piotroski score from whatever signals are available.

    Returns (ratio, detail) where ratio = passed / available, or (None, ...) if no signal
    could be computed. Each signal is only counted when its inputs are present, matching the
    single-snapshot rule from the spec.
    """
    signals: dict[str, bool] = {}

    net_income = fundamentals.get("net_income")
    if net_income is not None:
        signals["roa_positive"] = net_income > 0

    ocf = fundamentals.get("operating_cash_flow")
    if ocf is not None:
        signals["ocf_positive"] = ocf > 0

    if ocf is not None and net_income is not None:
        # Accrual quality: cash earnings should exceed accounting earnings.
        signals["accrual_quality"] = ocf > net_income

    gross_margin = fundamentals.get("gross_margin")
    if gross_margin is not None:
        signals["positive_gross_margin"] = gross_margin > 0

    revenue_growth = fundamentals.get("revenue_growth")
    if revenue_growth is not None:
        signals["revenue_growing"] = revenue_growth > 0

    if not signals:
        return None, {"available": 0, "passed": 0, "signals": {}}

    passed = sum(1 for v in signals.values() if v)
    available = len(signals)
    return passed / available, {
        "available": available,
        "passed": passed,
        "signals": signals,
    }


def tier2_sprinkle_sauce(
    fundamentals: dict,
    *,
    max_peg: float = MAX_PEG,
    min_fcf_yield: float = MIN_FCF_YIELD,
    piotroski_ratio: float = PIOTROSKI_RATIO,
) -> TierResult:
    """Tier 2 — the Sprinkle Sauce fundamental gates: PEG, FCF yield, Piotroski.

    Thresholds are PARAMETERS defaulting to the module constants, so this module keeps working with
    no database anywhere in sight — src/ is imported by CLI tools (daily_scan.py) as well as by the
    backend. The backend passes the operator's tuned values; anything else gets the Wasden defaults.
    Reading settings in here would hand every command-line consumer a Postgres dependency to satisfy
    a preference only the dashboard has.
    """
    reasons: list[str] = []
    metrics: dict = {}

    # PEG: positive and below threshold. Negative PEG => negative earnings growth => excluded.
    peg = fundamentals.get("peg")
    metrics["peg"] = peg
    if peg is None:
        reasons.append("PEG unavailable")
    elif peg <= 0:
        reasons.append(f"PEG {peg:.2f} <= 0 (negative earnings growth)")
    elif peg >= max_peg:
        reasons.append(f"PEG {peg:.2f} >= {max_peg}")

    # FCF yield (percentage units).
    fcf_yield = fundamentals.get("fcf_yield")
    metrics["fcf_yield"] = fcf_yield
    if fcf_yield is None:
        reasons.append("FCF yield unavailable")
    elif fcf_yield <= min_fcf_yield:
        reasons.append(f"FCF yield {fcf_yield:.2f}% <= {min_fcf_yield}%")

    # Piotroski (proportional single-snapshot).
    ratio, detail = _piotroski_proportional(fundamentals)
    metrics["piotroski"] = detail
    if ratio is None:
        reasons.append("Piotroski: no computable signals")
    elif ratio < piotroski_ratio:
        reasons.append(
            f"Piotroski {detail['passed']}/{detail['available']} "
            f"({ratio:.2f} < {piotroski_ratio:.2f})"
        )

    return TierResult(passed=not reasons, reasons=reasons, metrics=metrics)


def _composite_score(t1: TierResult, t2: TierResult) -> float:
    """Pre-Wasden ranking score for survivors. Higher = more attractive on the raw numbers.

    Deliberately simple and transparent — it only *orders* the candidates the agent then judges
    with the Wasden lens. Rewards cheap PEG, fat FCF yield, and strong Piotroski.
    """
    peg = t2.metrics.get("peg") or MAX_PEG
    fcf_yield = t2.metrics.get("fcf_yield") or 0.0
    pio = t2.metrics.get("piotroski", {})
    pio_ratio = (pio.get("passed", 0) / pio["available"]) if pio.get("available") else 0.0

    # Normalize each into roughly [0, 1] and weight.
    peg_score = max(0.0, (MAX_PEG - peg) / MAX_PEG)          # cheaper PEG -> higher
    fcf_score = min(1.0, fcf_yield / 10.0)                    # 10%+ FCF yield caps out
    return round(0.4 * peg_score + 0.4 * fcf_score + 0.2 * pio_ratio, 4)


def screen_ticker(
    ticker: str,
    fundamentals: dict,
    min_market_cap: float = MIN_MARKET_CAP,
    *,
    max_peg: float = MAX_PEG,
    min_fcf_yield: float = MIN_FCF_YIELD,
    piotroski_ratio: float = PIOTROSKI_RATIO,
) -> ScreenResult:
    """Run the full fundamental screen for one ticker.

    Stops at the first failing tier (cheap, and the fail reason is the most informative one).
    """
    t1 = tier1_liquidity(fundamentals, min_market_cap=min_market_cap)
    tiers = {"liquidity": t1}
    if not t1.passed:
        return ScreenResult(ticker, False, "liquidity", tiers, None)

    t2 = tier2_sprinkle_sauce(
        fundamentals,
        max_peg=max_peg,
        min_fcf_yield=min_fcf_yield,
        piotroski_ratio=piotroski_ratio,
    )
    tiers["sprinkle_sauce"] = t2
    if not t2.passed:
        return ScreenResult(ticker, False, "sprinkle_sauce", tiers, None)

    return ScreenResult(ticker, True, None, tiers, _composite_score(t1, t2))
