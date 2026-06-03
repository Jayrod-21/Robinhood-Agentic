# Execution log — 2026-06-03 — first full slate deployed

**Account:** Robinhood "Agentic" ••••4025 (cash). **Start:** $100.00 cash, 0 positions.
**Order type:** dollar-amount market, regular hours, GFD. All fractional.

## Order timeline

1. **Pre-trade:** confirmed all 7 fractional-tradable (`get_equity_tradability`); ran `review_equity_order`
   on all 7 — clean (no broker alerts).
2. **First batch (7 orders placed):** TSM filled immediately. The other 6 were **rejected** with:
   *"We're required to have you answer some questions about your investing goals..."* — Robinhood's
   per-account **investor-profile gate**, legally required before an account's *second* trade.
3. **Blocker diagnosis:** re-tried (still blocked, confirmed live). The `applink.robinhood.com` link
   the API returns is a **mobile-app deep link** (no-op in a desktop browser, no push notification) —
   the profile must be completed inside the Robinhood app (switch to the Agentic account → tap Buy →
   questionnaire appears). Jared completed it.
4. **Second batch (6 orders):** after profile completion, QCOM test order cleared, then the remaining
   5 placed — all filled.

## Fills

| Ticker | Shares | Avg fill | Cost | Order id |
|---|---|---|---|---|
| TSM  | 0.049910 | $440.79 | $22.00 | 6a204a52-70d9-4484-9fed-442f1c0dfebc |
| QCOM | 0.024050 | $249.48 | $6.00  | 6a204c7e-ce01-44dc-9fba-68cf1dc40d1b |
| VST  | 0.095693 | $156.75 | $15.00 | 6a204c95-fced-41c7-b238-df82c7a80af1 |
| NVDA | 0.059894 | $217.05 | $13.00 | 6a204c98-ee1a-49f9-961b-09902051c729 |
| V    | 0.038704 | $310.05 | $12.00 | 6a204c9a-db29-4de3-9c7d-ae24e0643bc1 |
| CVX  | 0.057579 | $191.04 | $11.00 | 6a204c9c-c845-4dd1-a718-82e578a53619 |
| GEV  | 0.009242 | $973.82 | $9.00  | 6a204c9e-0ad9-4784-884b-fa0a78960cea |

## Resulting portfolio
- **Equity $87.87 + Cash $12.00 = $99.87 total.**
- Entry friction ≈ **$0.13** (bid/ask spread across 7 fractional market buys ≈ 0.13%) — the expected small-account drag.
- Cash 12% (within the 10-20% floor). PLTR skipped (rental-only, no catalyst) → its 2% stayed in cash.

## Lessons (for the loop + the future app)
- **Per-account investor-profile gate** must be completed in-app before trade #2. The future 24/7 app
  should detect this state and surface it to Jared rather than silently failing a batch.
- **Don't hand a user an `applink.*` deep link as a desktop action** — it's app-only. Verify a remediation
  path works before presenting it.
- **Batch placement is fine** but a rapid 6th call got rate-limited (HTTP 429, ~11s) — the app should
  space order placement slightly.
