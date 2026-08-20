"""Agentic Robinhood dashboard backend.

FastAPI service that powers the 3b live dashboard:
- reads the account of record from the broker (a live Alpaca paper read when credentials are
  configured, otherwise the fallback snapshot file that bin/alpaca_sync.sh keeps current) and
  overlays live FMP marks,
- runs the real Sprinkle Sauce screen as a streamed pipeline,
- runs a live bull/bear + jury debate via the Anthropic API,
- records theses, exits and lessons to the journal, and scores debate calls for calibration.

The account view is strictly read-only — there is no order-placement code path here.
"""
