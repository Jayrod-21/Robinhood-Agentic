"""Robinhood Agentic Trader — core package.

Holds the daily scan (a lean, self-contained re-implementation of the Wasden Watch
"Sprinkle Sauce" screen, fed by FMP fundamentals) and the Alpaca client (alpaca.py) that
serves the account of record when credentials are configured — the Robinhood MCP snapshot
file remains the fallback account source. See ../docs/AGENTIC_ROBINHOOD_v1.md for the
operating charter and ../reference/SOURCES.md for the canonical Wasden material in 3a.
"""
