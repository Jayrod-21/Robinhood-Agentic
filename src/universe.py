"""Seed universe for the daily scan.

Per the charter, the universe is large-cap quality PLUS liquid, volatile mid/small-caps that
still carry real fundamentals, with a tilt toward Wasden's bucket-4 structural themes (AI, aging,
energy). This is a deliberately small, curated seed for the first build — fast to scan, easy to
reason over. It will grow into a fuller S&P 500 + screened mid/small list over time.

Grouped only for human legibility; the scanner flattens it.
"""

WATCHLIST: dict[str, list[str]] = {
    # Large-cap quality / cash machines
    "large_cap_quality": ["AAPL", "MSFT", "GOOGL", "META", "UNH", "JPM", "V", "COST"],
    # Bucket 4 — AI / semis / compute theme
    "theme_ai": ["NVDA", "AMD", "AVGO", "TSM", "MU"],
    # Bucket 4 — energy (Wasden's home turf)
    "theme_energy": ["XOM", "CVX", "OXY", "SLB"],
    # Bucket 4 — aging / healthcare structural
    "theme_aging": ["LLY", "ABBV", "ISRG"],
    # Liquid, more volatile mid/small — the aggression sleeve
    "volatile_midsmall": ["SOFI", "RIVN", "PLTR", "CELH", "ELF"],
}


def flat_universe() -> list[str]:
    """All tickers, de-duplicated, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for group in WATCHLIST.values():
        for t in group:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out
