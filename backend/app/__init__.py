"""Agentic Robinhood dashboard backend.

FastAPI service that powers the 3b live dashboard:
- reads the volume-mounted account snapshot and overlays live yfinance marks,
- runs the real Sprinkle Sauce screen as a streamed pipeline,
- runs a live bull/bear + jury debate via the Anthropic API,
- queues account refreshes for the host-side refresh daemon.

The account view is strictly read-only — there is no order-placement code path here. Live
trading stays with the in-session Claude agent driving the Robinhood MCP (see PROJECT.md).
"""
