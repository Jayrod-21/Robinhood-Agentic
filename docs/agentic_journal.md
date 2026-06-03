# Agentic Robinhood — Trading Journal

> Self-learning substrate. The agent reads this at the start of every cycle and appends to it.
> One entry per decision. On close, the entry is updated with outcome + lesson.
> Account: Robinhood "Agentic" ••••4025 (cash). See `docs/AGENTIC_ROBINHOOD_v1.md`.

## Account Ledger

| Date | Event | Account Value | Cash | Notes |
|------|-------|--------------|------|-------|
| 2026-06-03 | Baseline | $100.00 | $100.00 | Clean slate, zero positions. Charter v1 created. |
| 2026-06-03 | First trade (TSM) | $99.99 | $78.00 | TSM filled 0.04991 sh @ $440.79 = $22.00. Other 6 slate orders BLOCKED (investor-profile gate). |

## Open Positions (live)
| Ticker | Qty | Avg cost | Cost basis | Thesis role | Stop (mental -20%) |
|--------|-----|----------|-----------|-------------|--------------------|
| TSM | 0.049910 | $440.79 | $22.00 | Compute anchor (THESES.md) | ~$352.6 |

## Scan Log

### 2026-06-03 — first full scan (25 names, no trade yet)
Screen survivors (7): TSM, ABBV, CVX, UNH, OXY, V, SLB. Prices @ ~10:22 ET.
Agent Wasden-lens ranking + narratives (deep research w/ news+ecosystem):
- **UNH $385.65** — TOP. Bucket-5 special situation / fallen-angel recovery. 2025 crash (DOJ probe, MA cost shock, Witty->Hemsley, Thompson aftermath) reverting: CMS 2027 MA rate +2.48% (vs +0.09% prelim) +11% pop; Q1 beat + guide raise (FY EPS >$18.25), MCR 83.9% improving; DOJ CIVIL suit near dismissal (Special Master). Cheap PEG 1.32/FCFy 5.1%. Lone real tail = DOJ CRIMINAL probe (binary) -> gate sizing. Catalyst: Q2 ~Jul 10.
- **TSM $439.86** — Bucket-4 AI structural + best quality. FCFy 31.5%, Pio 5/5, Strong Buy PT ~$465, capacity sold out thru 2028, Nvidia now #1 cust. Tails exogenous: Taiwan-China, US tariffs. Catalyst: Q2 ~mid-Jul.
- **V $309.80 (-2.4% today)** — buyable FEAR. Down on stablecoin/GENIUS-Act disintermediation theme (Walmart/Amazon own-stablecoin WSJ report), NOT fundamentals (Q2 rev +17%, $20B buyback). BUT June 17 GENIUS Act vote = binary near-term. Wasden buy-when-fearful, but event-gated -> smaller size.
- **CVX $190.28** — best-QUALITY oil. PEG 0.81, Hess/Guyana closed Apr28 (won arb vs XOM), Q1 beat. Integrated = refining hedge, least oil-leveraged of the 3 energy names.
- **ABBV $217.61** — cheapest PEG 0.59, Humira cliff bridged (Skyrizi+Rinvoq), quality compounder. Lower vol = less aggressive. IRA/Botox litigation overhang.
- **OXY $59.86** — Buffett 26.6%, deleveraging fast ($13.3B), OxyChem SOLD to Berkshire Jan'26 (now pure E&P). But HOLD consensus, PT ~$61 ≈ spot = capped. Oil-leveraged.
- **SLB $56.42** — WEAKEST. Least cheap (PEG 1.88), most oil-price-LEVERAGED/cyclical, EPS -28% YoY, Brent ~$63 & falling, upstream capex down 2nd yr. Quality-at-cyclical-peak-risk. Skip.

**Cross-cutting (Wasden risk #3 correlation):** CVX+OXY+SLB = ONE oil-price bet. Crude elevated NOW (WTI ~$95/Brent ~$96) on geopolitical premium, but bearish 2H'26 (EIA $89 Q4, JPM Brent ~$60). Buying oil at a geopolitical high ≈ buying greed -> at most ONE energy name, tactical, tight exit. UAE exited OPEC eff May 1'26.

**Proposed starter (NOT yet executed, bootstrap=confirm-each):** UNH 25% / TSM 25% / V 20% / CVX 15% / cash 15%. 4 concentrated names, no oil cluster, within 10-20% cash floor. Awaiting Jared go/adjust.

### 2026-06-03 — 4-position debate (12-agent workflow) outcome
Judge scores (3 judges): Catalyst-Event 78/78/78 (winner) > Value-Concentration 70/58/66 > Risk-Skeptic 62/62/61 > High-Beta-Momentum 40/70/47.
**VERDICT on proposed slate: PARTLY.** UNH/TSM/V/CVX = right thing to OWN (survival core, kills oil cluster) but NOT best path to 25-40%/mo as buy-and-hold. The "how" (trade dated catalysts, exit into gap) matters more than the "what."
**HONEST realistic return: +3% to +8%/mo expected, wide distribution.** 25-40% = right-tail outcome ~1 in 3-4 months when a dated catalyst fires clean — NOT a monthly mean. Bad months -10% to -25%.
**RECOMMENDED PATH (blend):** dated catalysts on cheap-floor names — buy fear, exit into the gap, never hold an uncapped binary through resolution. Engine=Catalyst timing, floor=Value cheapness, seatbelt=Risk caps.
**Consolidated rec slate:** TSM 25 / UNH 15-18 (CAPPED for DOJ-criminal tail, was 25-30) / V 18 (STAGED catalyst trade not hold, exit into Jun17 pop, stop $292) / CVX 12 (lone oil ballast) / PLTR 12 (high-beta PAYLOAD, dated-catalyst-only, scale-out, the one line that can pay 30% in a catalyst month) / cash 15.
**Best NEW ticker: PLTR** (beta ~2.6, ±15-25% on earnings = the instrument that physically moves 30%; size small + dated-only). NVDA = bench substitute (never both = 1 AI-beta bet). MU lower-conviction.
**Binary-sizing rule:** cap any true criminal/regulatory-binary name (UNH, PLTR) + SIZE AS A COIN FLIP — the cap, never the stop, is the defense (gaps don't honor stops on $100 fractional). Exit BEFORE resolution or accept the gap.
**DO FIRST (judge consensus):** stage V ~$10 (10%) near $309 ahead of ~Jun17 GENIUS vote; $300 add-level + $292 hard stop pre-written before clicking. Nearest dated catalyst, genuine fear, sets the discipline. STILL awaiting Jared go (bootstrap).

### 2026-06-03 — FORWARD LAYER built + theses written + CATALYST CORRECTION
Built forward-thinking layer: `docs/THESIS_FRAMEWORK.md` (top-down 5-bucket pass + per-name forward-thesis template) + `docs/THESES.md` (living forward theses for TSM/PLTR/UNH/V/CVX). Deep forward research (4 agents, ~160k tok).
**!! CORRECTION:** "V June 17 GENIUS Act vote" was WRONG — GENIUS Act passed/signed 2025. Live 2026 catalyst = CLARITY Act (Senate Banking advanced 15-9 May'26, floor vote pending, NO fixed Jun17 date). Walmart/Amazon own-stablecoin = Jun 2025 WSJ report, no at-scale launch since. The "do first: stage V into Jun17" action above is VOID as written — V thesis still valid (co-option > disintermediation, estimates rising while multiple compresses) but there is no imminent dated June 17 cliff. Forward layer caught it.
**Forward thesis convictions:** TSM HIGH (monopoly raising prices into sold-out demand; US intel downgraded Taiwan-2027 invasion risk Mar'26 = shrinking discount not repriced). UNH MED-HIGH (civil-near-won undercuts criminal theory; watch Jul 7 civil-trial date + Q2 MCR). V MED-HIGH (co-opts stablecoins via USDC settlement; own the headline-beta fear). CVX MED-HIGH (NOT oil bet — quality ballast + under-priced AI-power→natgas leg, 2.5GW W.Texas plant 2027). PLTR MED (business accelerating but ~115x P/S prices perfection; inverse of TSM; downside under-appreciated; dated-catalyst rental only, size small).
**Key expectations-gap framing now per name (the edge):** see THESES.md. Next: re-decide first trade w/ corrected V catalyst; still bootstrap (Jared confirms).

### 2026-06-03 — Expanded universe + DATACENTER-BACKLASH reframe + UNH DEMOTED (5 research agents)
Jared flagged: datacenter buildout backlash; NVDA agentic computer; QCOM AI integration; UNH skepticism ("heard it, hasn't paid"); wants nuclear + quantum.
**DATACENTER BACKLASH = REAL & big:** local/state ban efforts 8->78 in 1yr (~10x), cancellations 6->25, $60B+ blocked, PJM power +75.5% YoY. Constraint shifted chips->MEGAWATTS. Capex REROUTES (not stops) -> power-gen, grid, cooling, behind-the-meter, EDGE. Durable (interconnect queues + transformer lead times physical). 2nd-order WINNERS: GEV (grid/turbines, $150B backlog, #1), VRT (cooling, backlog +109%), CEG/VST (nuclear), ETN, CCJ. LOSERS: NVDA (most DC-exposed), hyperscalers, pure DC REITs/contractors.
**NEW THESES added to THESES.md:** NVDA HIGH (both-sides hedge: DC monopoly + RTX Spark agentic edge PC = wins if cloud throttles; fwd P/E ~22-25 vs ~46 avg). QCOM MED (deep value ~14x, edge AI + AI200/AI250 inference + HUMAIN ~$1B + auto; cheapest large-cap edge play). GEV HIGH (cleanest power-reroute, hard backlog). VST HIGH (best nuclear risk/reward: ~18x, FCFy 7.2%, Meta 2,600MW PPAs not in guidance; aggression WITH cash-flow floor). CCJ MED-HIGH (uranium, uncorrelated to SMR-exec risk). IONQ LOW = lottery only ($5-10 max, expect to lose; quantum commercialization 2029-2035).
**UNH DEMOTED (skeptic vindicated):** -37% from ATH, 19mo underwater, stalled lower highs ($404->$378), **BERKSHIRE EXITED entire stake Q1'26 (disclosed May15)**. Overhang stock — open EXPANDING DOJ CRIMINAL probe + active antitrust (Optum breakup tail) caps the multiple; MA membership shrinking 1.3-1.4M; ~21x fwd, only ~4% to PT. = resolution trade not forward trade. OFF core. (Note: 2027 MA rate data conflicts across rounds: +2.48% vs +0.09% — unresolved.)
**Reframe implication:** forward aggressive slate should tilt toward POWER+EDGE reroute (TSM/NVDA/QCOM + GEV/VST/CCJ + CVX gas) over the original quality-value slate. Awaiting Jared: rebuild slate around reframe? Still bootstrap, no trade placed.

### 2026-06-03 — ALLOCATION debate (12-agent workflow) -> TARGET SLATE set (see SLATE.md)
Philosophy scores: Reframe-Barbell 80/82/84 (WON) > Correlation-Risk 76/74/76 > Catalyst-Payload 71/71/71 > Concentrated-Conviction 68/62/62 (too one-factor). 3 judges converged within pct points.
**FINAL CONSENSUS ALLOCATION ($100):** TSM 22 / VST 15 / NVDA 13 / V 12 / CVX 11 / GEV 9 / QCOM 6 / PLTR 2 / CASH 10.
**Unanimous EXCLUSIONS:** IONQ + OKLO + NuScale = 0% (no expect-to-lose lottery in no-ruin $100 book — kills earlier "tiny optional IONQ"). CCJ dropped (deepened AI-power factor, lost most on down-tape). UNH off (prior demotion).
**Correlation verdict:** ~60% of book = ONE AI-buildout-capex bet; barbell hedges which END wins, not cycle rollover. Only V+CVX (23%) + cash (10%) = ~33% truly off-factor. Deliberate concentration = the aggression.
**Sizing rules:** PLTR cap 2% rental-only; -20% mental stop/name (no averaging down); trim winners past 1.3x; on capex guide-down cut GEV/NVDA first + raise cash to 20%; keep V+CVX >=20% always.
**Honest return:** ~+4-6%/mo median; 25-40% = right-tail 1-in-3-4mo; left tail -12 to -18% in air-pocket.
**Live tape during debate (intraday Jun3):** TSM ~$441, NVDA $215.99 (-3.1%), GEV $974.53, VST $156.68; PLTR/IONQ/OKLO/CCJ all red = risk-off day.
**FIRST TRADE (judge consensus):** TSM ~$22 (anchor, lowest-variance). Still bootstrap — awaiting Jared confirm. No order placed yet.

## Open Positions

_None yet._

## Decision Log

### 2026-06-03 — BUY TSM (first live trade) ✅ FILLED
- Order: market, dollar_amount $22.00, ref_id 1111...1111, order id 6a204a52-70d9-4484-9fed-442f1c0dfebc.
- Fill: 0.049910 sh @ $440.79 avg = $22.00. Account → $99.99 value / $78 cash.
- Thesis: compute anchor — AI-foundry monopoly, sold out thru 2028, shrinking Taiwan-invasion discount (US intel downgrade Mar'26). HIGH conviction, lowest-variance HIGH name = top weight (22%). See THESES.md.
- Planned exit: -20% mental stop ~$352.6 (re-underwrite, don't average down); trim past ~1.3x; target = invasion-discount re-rate, watch Q2 ~mid-July.

### 2026-06-03 — remaining 6 slate orders BLOCKED (not placed)
VST $15 / NVDA $13 / V $12 / CVX $11 / GEV $9 / QCOM $6 all rejected: Robinhood requires investor-profile completion before the account's SECOND trade. Reviews were clean (fractional-tradable, no alerts). Pending Jared completing profile:
https://applink.robinhood.com/investment_profile?account_number=542574025&context=second_trade
Once done: place the 6 (will leave ~$12 cash = 12%, within floor). Quotes at attempt: VST $156.67, NVDA $216.32, V $309.30, CVX $190.59, GEV $974.20, QCOM $250.75.

## Lessons Learned

_Populated as positions close. Each lesson links the outcome back to the thesis so the loop compounds._
