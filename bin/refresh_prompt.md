Pull live Robinhood Agentic account -> write dashboard snapshot. Read-only. No trades.

Steps:
1. MCP `get_portfolio` account_number=542574025.
2. MCP `get_equity_positions` account_number=542574025.
3. Get current UTC time (ISO-8601, e.g. run `date -u +%Y-%m-%dT%H:%M:%SZ`).
4. Overwrite file `/root/Jared/3b. Robinhood Agentic/data/account_snapshot.json` with EXACTLY this schema:

{
  "schema_version": 1,
  "source": "robinhood-mcp",
  "generated_at": "<UTC ISO-8601>",
  "account": {
    "number_masked": "••••4025",
    "nickname": "Agentic",
    "total_value": <portfolio.total_value float>,
    "equity_value": <portfolio.equity_value float>,
    "cash": <portfolio.cash float>,
    "buying_power": <portfolio.buying_power.buying_power float>,
    "currency": "USD"
  },
  "positions": [
    {"symbol": "<sym>", "quantity": <float>, "average_buy_price": <float>, "intraday_quantity": <float>}
    // one per open equity position
  ]
}

Rules:
- Numbers = JSON numbers (not strings). Strip RH string-quotes.
- One positions entry per open position from get_equity_positions. Empty list if none.
- Use account 542574025 ONLY. Never place/modify any order.
- After writing, print: DONE
