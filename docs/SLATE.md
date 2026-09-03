# Target Slate — account 1

> **Slate status: NOT IN FORCE** — superseded 2026-08-17 when the account of record moved to the Alpaca paper book; no allocation debate has run against it since.
>
> **What that means, and what it does not.** This document is retained in full as the written
> record of the 2026-06-03 allocation debate. It is no longer a claim about what the book should
> hold today, so reconciliation does not apply it, and the position and market-context pages do not
> present its weights as targets. Delete the status line above to put it back in force.
>
> **Why it was retired.** The weights below were decided against a $100 Robinhood book. The fifteen
> positions the Alpaca paper account holds today were placed by `bin/seed_paper_book.py` — an owner
> seeding an equal-dollar basket ($500 a name) so the marking job had something to value, which that
> script is explicit is "not the agentic loop deciding anything" and must not be read as the
> strategy's track record. Reconciling one against the other is not a weaker comparison; it is a
> wrong one, and it produced eighteen findings and two guardrail breaches at the top of every
> morning report — every one of them an artifact of comparing two different books.
>
> **What replaces it.** Nothing, until an allocation debate runs against the current book and
> produces a slate for it. Until then the cycle reads positions from the broker and judges them on
> their own merits, and the reconciliation section says "no slate in force" rather than inventing a
> diff. See issue #22.

> **This file is the slate for ACCOUNT 1 only.** Alpaca paper `••••I1PN` — the agentic debate book.
>
> **Documented book: $100,000.** That line is parsed (`_DOCUMENTED_BOOK_LABELLED`) and reported as
> `documented_book_value`, so reconciliation can say when the live account has drifted from the size
> the slate assumes — unrecorded deposits, or a broker migration. **Keep the label.** The value used
> to be scraped out of a prose sentence naming the account and its size, so rewriting that sentence
> — which adding this per-account header did — turned it into `None` with nothing noticing. Other accounts have their own slates under `docs/slates/account-<N>.md`, or no slate
> at all, and reconciliation never applies one account's targets to another's holdings. See
> [Which slate governs which account](#which-slate-governs-which-account) below.
>
> Output of the 2026-06-03 allocation debate (12-agent workflow; Reframe-Barbell philosophy won
> 80/82/84). Updated each cycle; executed in bootstrap mode (an owner confirms every order). Theses
> live in `THESES.md`.
>
> **Account of record (since 2026-08-17): Alpaca paper `••••I1PN`, $100,000.** The debate that
> produced these weights was run against a $100 Robinhood book; the percentages carry over
> unchanged, because they were always an allocation rather than a dollar plan. The dollar column
> below is restated on the current book size.
>
> **These targets have never been executed.** What the broker holds today is the seeded basket
> described under *Holdings that are not this slate* — an owner action taken to give the marking job
> and the performance page something real to value. Reconciliation therefore reports most of this
> slate as missing and most holdings as unrecorded, and that is the correct reading, not a fault to
> tune away.

## Allocation (as of 2026-06-03) — NOT yet executed

| Ticker | % | $ on $100k | Role | Why that size |
|--------|---|-----------|------|---------------|
| **TSM**  | 22 | $22,000 | Compute anchor | Lowest-variance way to be long all silicon (builds NVDA's chips too); top weight = lowest blowup risk |
| **VST**  | 15 | $15,000 | Power leg + floor | FCFy 7.2%, ~18x, Meta 2,600 MW PPAs not in guidance = free option; safest AI-power beta, sized above GEV |
| **NVDA** | 13 | $13,000 | Convexity engine | Wins cloud AND edge (RTX Spark), fwd P/E ~22; sized below TSM = most backlash-exposed |
| **V**    | 12 | $12,000 | Off-factor diversifier | Payments/stablecoin, own cycle — doesn't ride AI-capex sentiment |
| **CVX**  | 11 | $11,000 | Off-factor ballast | Oil + natgas-to-AI-power leg (2.5 GW TX gas 2027); lowest beta to compute |
| **GEV**  | 9  | $9,000  | Power high-beta call | $150B backlog — inverse-sized for +235%/yr extension; conviction earns the thesis, not extra weight |
| **QCOM** | 6  | $6,000  | Cheap edge satellite | ~14x Snapdragon + AI200/AI250 inference; asymmetric, kept small |
| **PLTR** | 2  | $2,000  | Dated-catalyst rental | ~115x prices perfection; rental only, flat between prints |
| **CASH** | 10 | $10,000 | Dry powder | Hard floor; only truly uncorrelated asset; reload fund for an air-pocket |

Sum = 100%. Cash 10% (within [10,20]).

## Holdings that are not this slate

On 2026-08-17/18 an owner seeded the paper account with fifteen names at **$500 each** — an
equal-dollar basket chosen to give the marking job something real to value and the performance page
something to draw. `bin/seed_paper_book.py` records it, and the script says plainly that these fills
are not the strategy's track record.

They are written down HERE so that reconciliation's "unrecorded" rows have an explanation on file.
That is deliberately not the same as promoting them to targets: a position with a dollar amount but
no thesis is exactly what the sell-discipline rule exists to catch, and blessing fifteen of them by
editing the table above would switch that alarm off rather than answer it.

**Measured 2026-08-25** (account value $99,992.61, cash 92.51%):

| Held | Weight | Reconciles as |
|------|--------|---------------|
| NVDA | 0.48% | drifted (13% target) |
| VST | 0.48% | drifted (15% target) |
| V | 0.53% | drifted (12% target) |
| CVX | 0.49% | drifted (11% target) |
| QCOM | 0.50% | drifted (6% target) |
| AMD | 0.49% | unrecorded |
| BE | 0.49% | unrecorded |
| BRK.B | 0.50% | unrecorded |
| GLD | 0.53% | unrecorded |
| GM | 0.51% | unrecorded |
| ISRG | 0.46% | unrecorded |
| MSFT | 0.51% | unrecorded |
| QBTS | 0.47% | unrecorded |
| SVRA | 0.52% | unrecorded |
| TMO | 0.54% | unrecorded |

Five are also slate names, so they reconcile as **drifted** — held at ~0.5% of account value against
double-digit targets. The other ten reconcile as **unrecorded**. TSM, GEV and PLTR are **missing**
entirely: they were never seeded. Cash sits at 92.5% against a 10-20% band, which breaches by
design, because the slate has not been executed.

**The scheduled cycle now reports this every run** (`backend/app/services/reconcile_check.py`).
It reads OUT OF SYNC at the top of every cycle report and logs at ERROR, which is correct and
expected until the slate is either executed or replaced. It does not stop the cycle; the
`cycle_halt_on_desync` tunable decides that and is off by default.

TMO was originally written "TMOM", which is not a symbol on any venue this project can reach. It was
resolved with the owner as Thermo Fisher and seeded 2026-08-18, which is why it arrived a day after
the other fourteen.

## Which slate governs which account

Reconciliation resolves a slate **per account** and never falls back to another account's plan
(`backend/app/services/slate.py::slate_path_for`):

| Account | Slate file |
|---------|-----------|
| 1 (this one) | `docs/SLATE.md`, or `docs/slates/account-1.md` if that exists |
| N | `docs/slates/account-N.md` |
| any, with no such file | **none** — reconciliation reports "no documented slate" and diffs nothing |

An account with no slate is a normal state, not a fault: a testing book is not supposed to have
targets. What must never happen is account 4 being measured against account 1's targets — every row
would be a finding, the panel would be permanently red, and an operator would learn to skim past the
one place a real desync shows up.

To give an account targets, write `docs/slates/account-N.md` with a table in the same shape as the
one above. The parser reads rows matching `| **TICKER** | pct | ... |` and treats `CASH` as the
cash target rather than a position.

## Correlation verdict (Wasden risk #3)
~60% of the book (TSM+VST+NVDA+GEV+QCOM) is **one bet** — the AI-buildout capex cycle staying intact.
The chips-vs-megawatts barbell hedges *which end* of the reroute wins, NOT whether the cycle rolls over.
The live tape proved it: NVDA/TSM/VST/CCJ all bled red together (CCJ lost most → excluded). Only genuine
decorrelators = **V + CVX (23%) + cash (10%) = ~33% truly off-factor.** Concentration is deliberate — it's the aggression.

## Excluded by the debate (unanimous)
- **IONQ / OKLO / NuScale = 0%.** A name you "expect to lose" has no place in a no-ruin $100 book.
  (Overrides the earlier "tiny optional IONQ" idea — the panel killed it 3-0.)
- **CCJ** dropped — it deepened the AI-power factor and lost most on the down-tape; not a real diversifier.
- **UNH** off (demoted earlier — Berkshire exit + overhang).

## Sizing discipline
- **PLTR** hard-capped 2%, RENTAL ONLY — enter 3–5 days pre-catalyst, exit on the print regardless. (No imminent catalyst → may skip until Q2 ~Aug.)
- **Hard mental stop −20%/name** — re-underwrite, never average a broken thesis.
- **Trim any winner past ~1.3× target.**
- **On a confirmed AI-capex guide-down:** cut highest-beta legs first (GEV, NVDA), raise cash toward 20%.
- **Off-factor floor:** keep V + CVX ≥ 20% at all times.

## Honest expected return
Base case ~**+4% to +6%/month median**, wide distribution. **+25–40% is a right-tail outcome (~1 in 3–4 months)**,
firing only when a dated catalyst lands on a sized name. Left tail −12% to −18% in a capex air-pocket
(the ~60% AI-factor fuses toward 1.0) — what V+CVX+cash exist to cushion and reload against.

## Execution order (bootstrap — an owner confirms each)
1. **TSM ~$22** (anchor, lowest-variance entry) — first.
2. **VST ~$15**, then **NVDA ~$13** (scale on weakness).
3. Then V, CVX, GEV, QCOM. PLTR only near a catalyst.
