"""Daily scan runner — fetch the universe, run the Sprinkle Sauce screen, print a report.

This is the daily heartbeat of the agentic loop. It produces a ranked candidate list with
transparent pass/fail reasons; it does NOT trade and does NOT decide. The in-session Claude
agent then applies the Wasden lens (5-bucket framework, cheap/expensive, catalyst, exit) to the
survivors and proposes any action for human confirmation per the charter.

Usage:
    python -m src.daily_scan                 # scan the seed universe
    python -m src.daily_scan --min-cap 5e9   # enforce the classic $5B Wasden large-cap floor
    python -m src.daily_scan AAPL NVDA OXY    # scan an explicit ticker list
"""

from __future__ import annotations

import argparse
import logging

from src.data import fetch_fundamentals
from src.screen import MIN_MARKET_CAP, ScreenResult, screen_ticker
from src.universe import flat_universe

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# Default scan floor: relaxed below the classic $5B so the charter's liquid mid/small-cap
# aggression sleeve can clear Tier 1, while still excluding illiquid micro-caps.
DEFAULT_MIN_CAP = 2_000_000_000


def run_scan(tickers: list[str], min_market_cap: float = DEFAULT_MIN_CAP) -> list[ScreenResult]:
    """Fetch + screen every ticker. Tickers with no data are skipped (logged)."""
    results: list[ScreenResult] = []
    for ticker in tickers:
        fundamentals = fetch_fundamentals(ticker)
        if fundamentals is None:
            continue
        results.append(screen_ticker(ticker, fundamentals, min_market_cap=min_market_cap))
    return results


def format_report(results: list[ScreenResult], min_market_cap: float) -> str:
    """Render a human-readable scan report. Survivors first (ranked), then rejects."""
    survivors = sorted(
        (r for r in results if r.passed),
        key=lambda r: r.composite or 0.0,
        reverse=True,
    )
    rejects = [r for r in results if not r.passed]

    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("DAILY SCAN — Sprinkle Sauce fundamental screen")
    lines.append(f"scanned={len(results)}  min_market_cap=${min_market_cap:,.0f}  "
                 f"classic_floor=${MIN_MARKET_CAP:,.0f}")
    lines.append("=" * 64)

    lines.append(f"\nSURVIVORS ({len(survivors)}) — ranked, pre-Wasden:")
    if survivors:
        for r in survivors:
            ss = r.tiers["sprinkle_sauce"].metrics
            peg = ss.get("peg")
            fcf = ss.get("fcf_yield")
            pio = ss.get("piotroski", {})
            lines.append(
                f"  {r.ticker:<6} score={r.composite:<6} "
                f"PEG={peg if peg is None else round(peg, 2)} "
                f"FCFy={fcf if fcf is None else round(fcf, 1)}% "
                f"Piotroski={pio.get('passed', '?')}/{pio.get('available', '?')}"
            )
    else:
        lines.append("  (none — no candidate cleared the fundamental gates today)")

    lines.append(f"\nSCREENED OUT ({len(rejects)}):")
    for r in rejects:
        reason = r.reasons[0] if r.reasons else "unknown"
        lines.append(f"  {r.ticker:<6} failed @ {r.failed_tier}: {reason}")

    lines.append("\n" + "-" * 64)
    lines.append("Next: agent applies the Wasden lens to survivors (bucket, cheap/expensive,")
    lines.append("catalyst, exit-before-entry) and proposes any trade for human confirmation.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Sprinkle Sauce scan")
    parser.add_argument("tickers", nargs="*", help="explicit tickers (default: seed universe)")
    parser.add_argument("--min-cap", type=float, default=DEFAULT_MIN_CAP,
                        help=f"market-cap floor for Tier 1 (default {DEFAULT_MIN_CAP:.0e})")
    args = parser.parse_args()

    tickers = args.tickers or flat_universe()
    results = run_scan(tickers, min_market_cap=args.min_cap)
    print(format_report(results, args.min_cap))


if __name__ == "__main__":
    main()
