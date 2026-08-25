# Agentic Robinhood Trader — Operating Charter v1

> **Version:** 1.0 | **Created:** June 3, 2026 | **Owners:** Jared, Joe
> **Status:** Active — bootstrap phase
> This is the operating charter for the live agentic trading loop running on the
> Robinhood "Agentic" account. It is a self-contained project that *references* the
> Wasden Watch intelligence, screening, and governance from `3a. SpecialSprinkleSauce`
> (see `reference/SOURCES.md`), adapted for a small, aggressive, fully-agentic equities account.
> Read the Wasden framework (via `reference/SOURCES.md` → 3a `KNOWLEDGE_BASE_v2.md`) before acting.

> ## ⚠ What this charter says that is no longer true (as of 2026-08-25)
>
> This is a **dated charter**, kept as the record of what was agreed on June 3, 2026. Its principles
> — concentration over leverage, human confirmation on every order, no-ruin sizing — all still hold.
> Several of its *facts* do not, and they are listed here rather than edited in place, because
> silently rewriting a signed charter loses the thing a charter is for.
>
> | §  | Says | Actually |
> |----|------|----------|
> | 2  | Venue is Robinhood `••••4025`, $100 cash | Account of record is **Alpaca paper `••••I1PN`, ~$100,000**, since 2026-08-17 |
> | 2  | "the ONLY account the agent may trade" | **Multiple Alpaca paper accounts** are planned for testing; `GET /api/accounts` is the live registry, and each account reconciles against its own slate (`docs/slates/`) or none |
> | 2  | Cash account, T+1 settlement, no margin | Paper account, margin multiplier 1 — buying power == equity |
> | 3  | Market data: Robinhood MCP quotes, yfinance/Finnhub fundamentals | **FMP** for prices and fundamentals; yfinance survives only in the corporate-actions/delistings loader image |
> | 3  | Execution: Robinhood MCP `review` → confirm → `place` | Alpaca (`src/alpaca.py`); `ALPACA_BASE_URL` is the one variable separating paper from live, and execution refuses live unless explicitly enabled |
> | 5  | Risk constants retuned for a $100 account | The percentage rules carry over unchanged — they were always allocations. The **dollar** figures do not. |
>
> Everything below is the June 3 text. `PROJECT_HISTORY.md` has the full account of what changed and
> when; `README.md` has the current state.

---

## 1. Mission

Grow a small live equities account aggressively, using Cary Wasden's fundamentals-first
methodology to decide **what** to own and technical/catalyst timing to decide **when**,
with concentration — not leverage or day-trading churn — as the source of aggression.

- **Stretch target:** 25–40% return per month.
- **Honest framing:** this target is far above any sustainable long-run rate and is
  only pursuable because the account is small, intentionally high-risk, and treated as
  a learning vehicle ("fun account" in Wasden's own taxonomy). It is a *direction*, not
  a promise. Success is also measured by the quality of the decision loop and the
  lessons captured, not the dollar P&L alone.

## 2. Account

- **Venue:** Robinhood, account nickname "Agentic" (`••••4025`), type **cash**, `agentic_allowed: true`.
- **This is the ONLY account the agent may trade.** The default cash account (`••••5323`)
  and the managed margin account are `agentic_allowed: false` and are strictly off-limits.
- **Starting balance:** $100.00 (all cash) as of June 3, 2026.
- **Cash account** = no margin, no PDT concerns (and PDT is repealed effective June 4, 2026 regardless),
  but trades settle T+1 — proceeds from a sale are not re-investable until settlement.
- **Instruments:** equities only (the MCP exposes equities; no options/crypto/margin through the agent).
- **Fractional shares:** supported — a multi-name basket is feasible even at $100.

## 3. Operating Model — who does what

| Layer | Implementation |
|---|---|
| Intelligence (Wasden verdict, bull/bear, jury) | The in-session Claude agent, grounded in `KNOWLEDGE_BASE_v2.md`. No paid LLM API calls. |
| Market data | Robinhood MCP real-time quotes; free fundamentals (yfinance / Finnhub) for screening inputs. |
| Screening | Lean re-implementation of the Sprinkle Sauce tiers (`src/`), modeled on 3a's `screening_engine.py` + `sprinkle_sauce_spec.md`, fed from free yfinance data. |
| Risk & validation | Reuse risk constants and the SEPARATE pre-trade validation path, retuned for a $100 account (§5). |
| Execution | Robinhood MCP: `review_equity_order` → human confirm (bootstrap) → `place_equity_order`. |
| Memory / learning | `docs/agentic_journal.md` — every decision and outcome logged; read back at the start of each cycle. |

## 4. The Decision Loop (one cycle)

1. **Recall** — read `docs/agentic_journal.md` (open positions, recent lessons) and account state.
2. **Universe & screen** — run the Sprinkle Sauce fundamental screen to a short candidate list.
3. **Wasden lens** — for each candidate, apply the 5-bucket framework and core disciplines:
   is it cheap or expensive? what's the catalyst? where's the sell *before* the buy?
4. **Bull / Bear / (jury if split)** — argue both sides; on genuine disagreement, run the jury reasoning.
5. **Risk & pre-trade validation** — position sizing, cash floor, duplicate/sanity checks (§5).
6. **Decide & size** — concentrated conviction; respect the cash floor.
7. **Execute** — `review_equity_order` first; in bootstrap, get human confirmation before `place_equity_order`.
8. **Journal** — log thesis, bucket rationale, entry, planned exit, size. On close, log outcome + lesson.

## 5. Risk Rules (retuned for a $100 aggressive account)

> Base Wasden constants are tuned for a large, diversified book. For a $100 account meant to be
> aggressive, position sizing is loosened and concentration is allowed — but the cash floor and
> the sell-discipline rule are HARD.

- **Cash floor:** keep **10–20% in cash at all times** (Wasden: "always have cash"). Hard rule.
- **Max position size:** up to **~25%** of account value per name (concentration = the aggression lever),
  vs. the 12% institutional default. Revisit as the account grows.
- **Max names:** ~4–6 concentrated positions (not a sprawling basket on $100).
- **Sell discipline:** every entry records its exit (target and stop / invalidation) BEFORE the buy. Hard rule.
- **No averaging down** into a broken thesis — re-evaluate from scratch instead.
- **Friction awareness:** small account → spread/slippage matters. Avoid churn; trade only when the thesis warrants.
- **Drawdown guard:** if the account draws down past a set threshold or hits a consecutive-loss streak,
  halt new entries and escalate to an owner (mirrors the Wasden Watch shutdown discipline).

## 6. Wasden Alignment

- **Fundamentals pick the horse, technicals pick the moment.** Never trade on charts alone.
- Aggression lives in **buckets 4–5** (structural themes + special situations / mispricings), not in
  high-frequency trading of large caps. "Wrong 99 of 100 times, right once and sized correctly = transformational."
- Always: *is it cheap or expensive? what's the catalyst? how do I get out?*
- Buy fear, sell greed. Don't look past ~2 years. Ask why, why, why.

## 7. Autonomy Progression

- **Bootstrap (now):** agent recommends; **an owner confirms every order** before it places.
- **Supervised:** after a track record the owners trust, agent places within pre-agreed sizing/▸guardrails, reports after.
- **Scheduled autonomy:** a recurring runner (e.g. `/schedule`) wakes the agent on a cadence
  (pre-market scan, end-of-week review) to run the loop and log; the owners review on their own time.
- Autonomy widens only by an owner's explicit say-so. It can be revoked at any time.

## 8. Decisions (resolved June 3, 2026)

- [x] **Universe:** S&P 500 large-caps **+ liquid, volatile mid/small-caps** that still have real
      fundamentals. Aggression comes from concentration and security selection within a fundamental floor.
- [x] **Cadence:** **daily pre-market scan** — a morning review every trading day. CRITICAL: scanning
      daily does NOT mean trading daily. Most days the right answer is "no action." The daily run is for
      awareness and the Wasden morning-review discipline ("would I still buy today?"), not forced activity.
- [x] **Fundamentals data:** **yfinance** (free, no API key). Finnhub deferred (its free tier needs a key).
- [x] **First trades:** **bootstrap — an owner confirms every order** (review preview → approve → place) until
      a trusted track record exists.

### Still open
- [ ] Exact drawdown / loss-streak halt thresholds for a $100 account.
- [ ] When to flip from bootstrap (confirm-each) to supervised autonomy.
- [ ] Whether/when to stand up a scheduled runner (`/schedule`) vs. an owner invoking the daily scan manually.
