# Agentic Robinhood — Trading Journal

> Self-learning substrate. The agent reads this at the start of every cycle and appends to it.
> One entry per decision. On close, the entry is updated with outcome + lesson.
> Account: Robinhood "Agentic" ••••4025 (cash). See `docs/AGENTIC_ROBINHOOD_v1.md`.

## Account Ledger

| Date | Event | Account Value | Cash | Notes |
|------|-------|--------------|------|-------|
| 2026-06-03 | Baseline | $100.00 | $100.00 | Clean slate, zero positions. Charter v1 created. |
| 2026-06-03 | First trade (TSM) | $99.99 | $78.00 | TSM filled 0.04991 sh @ $440.79 = $22.00. Other 6 slate orders BLOCKED (investor-profile gate). |
| 2026-06-03 | Slate COMPLETE | $99.87 | $12.00 | Investor profile completed; remaining 6 filled. 7 positions live, ~$0.13 entry friction (spread). Cash 12%. |

## Open Positions (live, as of 2026-06-03)
| Ticker | Qty | Avg cost | Cost basis | Thesis role | Stop (mental -20%) |
|--------|-----|----------|-----------|-------------|--------------------|
| TSM  | 0.049910 | $440.79 | $22.00 | Compute anchor | ~$352.6 |
| VST  | 0.095693 | $156.75 | $15.00 | Nuclear power + cash-flow floor | ~$125.4 |
| NVDA | 0.059894 | $217.05 | $13.00 | Convexity (cloud+edge hedge) | ~$173.6 |
| V    | 0.038704 | $310.05 | $12.00 | Off-factor diversifier (payments) | ~$248.0 |
| CVX  | 0.057579 | $191.04 | $11.00 | Off-factor ballast (oil+gas→AI power) | ~$152.8 |
| GEV  | 0.009242 | $973.82 | $9.00  | Power-reroute high-beta call | ~$779.1 |
| QCOM | 0.024050 | $249.48 | $6.00  | Cheap edge satellite | ~$199.6 |
| **CASH** | — | — | **$12.00** | Dry powder (12%, within 10-20% floor) | — |

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

### 2026-06-03 — remaining 6 orders: initially BLOCKED, then FILLED ✅
First attempt rejected: Robinhood requires investor-profile completion before the account's SECOND trade (per-account; gated trade #2+). NOTE: the `applink.robinhood.com` link is a MOBILE-APP deep link — useless in desktop browser; profile must be completed in the Robinhood app (switch to Agentic acct ••••4025 → tap Buy on any ticker → questionnaire pops). After Jared completed it, all 6 filled:
- VST 0.095693 @ $156.75 = $15.00 · NVDA 0.059894 @ $217.05 = $13.00 · V 0.038704 @ $310.05 = $12.00
- CVX 0.057579 @ $191.04 = $11.00 · GEV 0.009242 @ $973.82 = $9.00 · QCOM 0.024050 @ $249.48 = $6.00
**SLATE FULLY DEPLOYED.** 7 positions, $87.87 equity + $12 cash = $99.87. Entry friction ~$0.13 (spread on 7 fractional market buys). All thesis/sizing per SLATE.md. Bootstrap complete — first full slate live.

### 2026-06-15 — 25-agent debate (best path to +20-40% in 1mo-1qtr) + $100 deposit
Jared funded the Agentic acct with a fresh $100 -> account ~$198.29 (cash ~$112, equity ~$86). Ran a 25-agent
debate (8 recon incl. macro/sentiment/IPO lane + 3 alpha-scouts, 8 strategy desks, 4 judges, 4 red-teamers, 1 PM synth).
Objective set explicitly by Jared: engineer the best path to +20-40% over the next month/quarter, not a passive rebalance.

**Book P&L since 6-03 entry (at 6-15 close):** V +4.5%, GEV +0.6%, TSM flat, NVDA -2.2%, VST -2.0%, CVX -5.5%, QCOM -11.6%.

**Engine = a dated-catalyst calendar, not buy-and-hold:** Jun-24 (MU FQ3 earnings AC + QCOM Investor Day 2:15pm ET,
same AI-semis factor = ONE bet), Jul-16 (TSM Q2, ~Jul-10 monthly-rev tell), Aug-22 (SVRA PDUFA, no AdCom).

**Honest return frame (return-realism judge + red-team enforced):** median ~+4-7%/mo. P(+20-40%) ~12-15% over 1mo,
~28-33% over 1qtr. Left tail: a Jun-24 correlated double sell-the-news = ~-8 to -12% book over the event window (not ruin;
bounded by small caps + 16.5% cash + off-factor V/CVX/SVRA legs).

**Red-team fixes adopted:** (1) NO CVX add — "oil bid" FALSIFIED (Brent ~$83-86, -20% off peak, Hormuz reopening <30d = bearish);
hold CVX as two-sided ballast only. (2) MU sized SMALL (9.5%) not large — at ALL-TIME HIGH $1,087.99, zero reversion cushion,
±20% implied; coin-flip, EXIT INTO THE GAP, NO dollar stop (cap is the defense). (3) QCOM add capped at $12 — "named hyperscaler
reveal" is UNVERIFIED moomoo chatter + pre-rallied +4.27%; don't chase green into the event, already own the free option at -11.6%.
(4) MU+QCOM AI-semis sleeve held to ~24.5%; SVRA added as the only genuinely uncorrelated leg. (5) Full GEV exit (cleanest
correlation cut, Jefferies PT cut 6-11). (6) MU "81% GM fabricated" attack REJECTED — verified real via SEC 8-K ($33.5B±750M
rev, $19.15±0.40 EPS, ~81.0% GM, HBM sold out 2026) — but 81% is the priced base case, hence small sizing.

**APPROVED TARGET SLATE (Jared approved full slate 6-15):** TSM 21.5 / QCOM 15 / VST 13.5 / MU 9.5 / NVDA 6.5 / V 6 /
SVRA 6 / CVX 5.5 / CASH 16.5. Per-name target/stop set BEFORE buy per sell-discipline.

**APPROVED ORDERS — staged for 2026-06-16 OPEN (market closed 6-15, fractional/dollar orders need regular hours):**
| # | Order | $ | Note |
|---|-------|---|------|
| 1 | BUY TSM | $20 | anchor, cheapest AI compute (PEG<1), lowest left-tail; target $510 / stop $375 mental |
| 2 | BUY VST | $18 | cheap megawatt theme, contracted cashflow; target $178 / stop $130 mental |
| 3 | BUY MU  | $18 | capped Jun-24 coin-flip, exit into gap; target $1,290 / cap-is-defense (no $ stop) |
| 4 | BUY QCOM| $12 | capped add into own Jun-24 Investor Day, ONLY if not gapping up pre-event; target $255 / stop $192 mental |
| 5 | BUY SVRA| $12 | LIMIT at/below $5.33 (bid 5.15/ask 5.49 = 6.4% spread); skip if won't fill cheap; Aug-22 PDUFA; target $10 |
| 6 | SELL GEV (full ~$9) | — | cleanest correlation cut; proceeds settle T+1, earmarked for any Jun-24 sell-the-news gap |
Deploy ~$80 fresh, leave ~$32 / 16.5% cash. **Jun-24 plan:** market-sell MU into the post-print gap (no round-trip),
trim QCOM into any Investor-Day pop.

**ORDERS QUEUED 2026-06-15 ~23:18 ET (Jared: "just do the trades, ok if active tomorrow morning"). All state=queued,
activate at the 6-16 open. Dollar-market orders queued gfd after-hours -> execute at open; SVRA gtc limit rests until fill.**
| Order | Qty/$ @ est | Order id |
|-------|-------------|----------|
| BUY TSM  | 0.045960 sh / $20 @ ~$435.09 | 6a30c055-66a7-40f8-8fb9-b228ea9faa78 |
| BUY VST  | 0.117340 sh / $18 @ ~$153.40 | 6a30c072-d9fd-4939-a929-fb2a3c25f993 |
| BUY MU   | 0.016800 sh / $18 @ ~$1071.11 | 6a30c074-7775-41ac-9d8f-70dd7d277973 |
| BUY QCOM | 0.054310 sh / $12 @ ~$220.92 | 6a30c079-7812-482a-9fe3-a9ddc180f937 |
| BUY SVRA | 2 sh LIMIT @ $5.33 GTC (rests; may not fill at open) | 6a30c083-6c94-4042-a074-c676df039424 |
| SELL GEV | 0.009242 sh (full exit) market | 6a30c085-934d-421c-b606-a6fa7bc7124c |
Net: all 6 placed by the `agentic` agent. Check fills at the open via get_equity_orders. SVRA limit may sit unfilled
if SVRA opens/stays above $5.33 — that is intended (skip-if-won't-fill-cheap).

**SCHEDULED CATALYST BRIEFINGS (cloud routines, email-only — Robinhood MCP is local-only, so cloud CANNOT trade):**
Two one-time claude.ai routines emit a researched Gmail draft to jaredmwilliams.me@gmail.com, then Jared executes in a LOCAL session.
- `trig_01ThfQRZHZU8SiZBE3xkKJNz` — "June 24 event-day brief" — fires 2026-06-24T12:30:00Z (6:30 MDT): QCOM Investor Day (12:15 MT) + MU earnings-tonight setup + exit-into-gap plan.
- `trig_01JHQ1JNRKbubR6qWKV5Ahzv` — "June 25 MU gap & exit plan" — fires 2026-06-25T12:00:00Z (6:00 MDT): MU post-print gap + the exit decision mapped to the actual gap; QCOM trim follow-up.
Manage at https://claude.ai/code/routines . Jul-16 (TSM) + Aug-22 (SVRA) not yet scheduled — add closer to the dates.

### 2026-06-16 — ALL 6 QUEUED ORDERS FILLED AT OPEN + Debate 3 (slate stress-test, 16-agent)
**Fills (6-16 open):** TSM +$20 @ $436.19 · VST +$18 @ $153.25 · MU +$18 @ $1,100.00 (at/above prior ATH) ·
QCOM +$12 @ $225.63 · SVRA 2sh @ $5.33 (limit filled) · GEV full exit 0.009242 sh @ $991.85 (+1.8%).
Account → $198.28 total / $42.54 cash (21.4%) / $155.74 equity. Slate fully redeployed.
**Live P&L day-1 (intraday ~10:47 MDT):** V +6.8%, VST +3.2% (winners) · TSM −1.5%, NVDA −3.7%, QCOM −5.5%,
CVX −6.1%, MU −4.5% (bought at ATH, bleeding before its own catalyst), SVRA −1.8%.

**Debate 3 (`logs/debates/2026-06-16-debate-3-slate-stress-test.md`, run wf_7a2b43d8-c51, ~608k tok).**
Judge tally: Correlation-Hawk 220/avg73 (WON) > Cut-The-Losers 194/65 > Hold-The-Line 171/57 > Ride-The-Winners 148/49.
**VERDICT: HOLD WITH TWEAKS.** Hawk won the diagnosis — ruin-relevant denominator is EQUITY not total acct:
TSM+MU+NVDA+QCOM = **56.5% of equity** in one record-crowded factor firing TWICE on Jun-24. But Return-Realist
+ Wasden judges' shared blindspot overrode a maximal de-gross: on a $198 book NO single re-trade moves
P(+20-40%) >~1-2 pts, and a −14% MU gap = −$2.41 = **1.2% of book** → 4 spread-paying sells over-engineer a
bounded tail. So: adopt the diagnosis, execute with the MINIMUM needle-moving trades.
**3 ORDERS (bootstrap — awaiting Jared go):** (1) TRIM MU ~$4 market (~23%, broken-ENTRY right-size, keep convex
HBM stub + gap-sell). (2) TRIM NVDA ~$3 market (~24%, cut 2nd-tightest beta; rejected 50% halving — best fundamentals).
(3) ADD V ~$6 LIMIT ~$331 (cure off-factor floor with stronger horse, funded by trims; do NOT chase >$333). Net cash ~flat → ~22%.
**HOLD:** TSM full (lowest-IV, no Jun-24 catalyst), QCOM (trim INTO Investor-Day pop not before), VST (let winner run, no add),
CVX (ballast, do NOT add oil into 4Mbpd glut), SVRA (sole true diversifier).
**Off-factor floor:** V+CVX = 11.7% of $198 vs 20% = a denominator artifact of the $100 deposit (NOT fresh risk).
REJECTED re-baseline-to-15% + counting VST as off-factor (VST = DEMAND side of same AI bet). +$6 V → ~14.6% today;
close rest from Jun-24 plan-of-record trims. No rule rewrite.
**Jun-24 playbook:** go in lighter (MU/NVDA trims), ~$33 dry powder. Intraday: trim QCOM $5-6 into Investor-Day pop.
After MU close print: market-sell the ~$13 stub INTO the gap (cap IS the defense). Clean+up → re-buy MU at a known
number / add TSM/VST. Down → bounded −8 to −12%, survivable, not forced.
**ESCALATION TRIGGER (Hawk dissent):** confirmed AI-capex guide-down before Jun-24 → escalate to full de-gross + 26%+ cash.
**Un-priced ruin path (Risk judge):** whole book ≈ ONE AI-buildout bet (semis supply + VST demand + CVX gas-to-power)
wearing 5 tickers; only SVRA truly uncorrelated. A confirmed AI scare hits supply AND demand together → could blow past
the −12% worst-case. Watch on any hyperscaler capex trim.

## Lessons Learned

_Populated as positions close. Each lesson links the outcome back to the thesis so the loop compounds._
