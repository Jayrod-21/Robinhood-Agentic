# Execution — scope and threat model

> **Status: PROPOSED. No code written.** This document exists to be argued with before anything is
> built. It describes making the dashboard able to place orders — the single largest change in this
> project's risk profile, and the first one that can lose money by working exactly as designed.

## 0. What changes

Today every path is read-only. `src/alpaca.py` calls `/v2/account` and `/v2/positions`, both GETs,
and `SECURITY.md` states the worst case from a compromised session is **disclosure of holdings, not
trading**. That sentence stops being true the day this ships, and it has to be rewritten in the same
change rather than left behind as a comforting falsehood.

**Blast radius today is fake money.** The account is Alpaca paper: $100,000, multiplier 1, no
positions. That is precisely why now is the right time — every mistake this design would make is
free right now and expensive later.

**The design must assume live anyway.** A paper-only execution path would be rewritten for live
under time pressure, which is the worst condition to redesign a money-moving system in.

---

## 1. What can go wrong

Ordered by how quietly it fails, because the loud failures are not the dangerous ones.

### 1.1 The same order twice
A double-click, a retry after a timeout, a browser refresh mid-request. The nastiest version is a
network timeout where the client never learns whether the order was accepted — retrying is both the
obvious recovery and a way to buy twice.
**Defence:** every order carries a `client_order_id` derived from the preview, not generated at send
time. Alpaca rejects duplicates server-side. A retry with the same id is a no-op rather than a
second position.

### 1.2 The wrong account
Paper and live differ by one environment variable. A live key with a paper URL fails loudly; a live
URL with live keys and a mislabelled UI does not.
**Defence:** `assert_paper()` on every execution path unless `EXECUTION_ALLOW_LIVE=true` is set
explicitly, and the response echoes which environment the order went to. Never inferred from the key
prefix — see the `/v2` incident in `src/alpaca.py`, where a string comparison labelled a paper
account as live.

### 1.3 Units
Shares versus dollars. `qty` versus `notional`. A 25% position on a $100 account is $25; on a
$100,000 paper account it is $25,000. The same code path, three orders of magnitude apart.
**Defence:** one unit at the API boundary (shares), the conversion done once and shown in the
preview in both units before confirmation. Tested against a real payload, the way the FMP
fraction-versus-percent mapping was.

### 1.4 A guardrail that blocks a valid trade
The failure mode this project has already paid for — roughly $4,000 to mis-set guardrails on another
system. A guard that silently refuses a correct order is worse than no guard, because the operator
learns to route around it.
**Defence:** guardrails are **tunable, observable, and overridable**, never silent. Every evaluation
writes a `guardrail_events` row with `rule_key`, `threshold`, `observed`, `action_taken`; an override
requires `override_by` and a non-empty `override_reason`. The preview shows every rule that fired
*and every rule that passed*, so "nothing blocked this" is visible rather than assumed.

### 1.5 A runaway loop
An agent, a retry loop, or a scheduled job placing orders faster than a human can read them. The
account is small; the API is not slow.
**Defence:** a hard per-window order cap enforced server-side, independent of any client. Tripping it
disarms execution and requires a human to re-arm. The cap counts *attempts*, not successes — a loop
that fails on validation still burns the budget.

### 1.6 Stale inputs
An order sized against positions or prices fetched minutes ago. The broker cache is 5 seconds and the
marks cache 45; both are fine for display and wrong for sizing.
**Defence:** the preview refetches account and positions uncached and stamps the preview with what it
saw. Confirming a preview older than its TTL is refused, not silently re-priced.

### 1.7 A compromised session
Auth is now real — Argon2id, TOTP, `__Host-` cookie, CSRF guard, per-client rate limits. But a
session that could previously only *read* would now be able to *trade*.
**Defence:** execution is armed separately from login. Holding a session is not sufficient; arming is
a distinct, audited, expiring act. This is the one place where the extra friction is the point.

### 1.8 Partial and rejected fills
An order can be accepted, partially filled, cancelled, or rejected minutes later. Recording intent as
though it were outcome puts the ledger permanently out of step with the account.
**Defence:** orders are recorded at **submission** with their broker id and status, and reconciled
from the broker afterwards. What we intended and what happened are separate columns, never merged.

---

## 2. Shape

### 2.1 A separate module, not a method
Execution lives in its own module with its own name. `src/alpaca.py` stays read-only and says so in
its docstring today; that promise should survive this change. A `place_order` quietly appended to the
read client is how a read-only guarantee erodes without anyone deciding to erode it.

### 2.2 Preview, then confirm
Two calls, mirroring the charter's `review_equity_order` → confirm → `place_equity_order`:

- `POST /api/orders/preview` — validate, size, evaluate every guardrail, refetch account state, and
  return a signed preview with a TTL. **Places nothing.**
- `POST /api/orders/confirm` — takes the preview id, re-checks arming and the guardrails that can
  change (price, buying power), submits with the preview-derived `client_order_id`.

The preview is the artefact an owner approves. It shows side, symbol, shares, estimated notional,
resulting position weight, cash after, and every guardrail with its threshold and observed value.

### 2.3 Armed, and it expires
Execution is disabled by default. Arming is an explicit POST, audited, with a TTL (proposed: 15
minutes) and a visible countdown. Disarming is instant and needs no confirmation — the emergency
control must never itself be gated.

`EXECUTION_ENABLED=false` in config is the outer switch: with it false, arming is impossible and the
endpoints answer 403. That is the setting that ships first.

### 2.4 Everything is audited
A new `orders` table: intent, preview, guardrail verdict, operator, broker id, submitted status,
later-reconciled status. Plus `guardrail_events` rows, which already have the right columns.

The audit is written **before** submission and updated after. An order that vanished between
submission and response must leave a record that it was attempted.

---

## 3. Guardrails, from the charter

`docs/AGENTIC_ROBINHOOD_v1.md` §5 already sets these. They become code with thresholds in config:

| Rule | Charter value | Proposed action |
|---|---|---|
| Cash floor | 10–20%, **hard** | block, override allowed with reason |
| Max position size | ~25% of account value | block, override allowed with reason |
| Max names | 4–6 concentrated | warn |
| Sell discipline: exit recorded before buy | **hard** | block on buys with no thesis/stop on record |
| No averaging down into a broken thesis | hard | warn (needs thesis state to judge) |
| Drawdown guard | halt new entries, escalate | block, no override |
| Order rate cap | *(new — §1.5)* | block, disarms |

Two the charter calls **hard** are the ones most likely to be argued with in the moment. They stay
overridable, because a blocked valid trade is the documented expensive failure — but an override is a
recorded act with a name and a reason attached, not a flag flip.

---

## 4. Explicitly out of scope

- **Autonomous placement.** Bootstrap means an owner confirms every order. Supervised mode is a
  separate change, gated on a track record that does not exist yet.
- **Options, shorts, margin.** Long equity only. `no_shorting` should be set true at the broker so
  this is enforced below us, not just by our own validation.
- **Order modification and cancellation.** Read-only status only in v1; cancel is a second change.
- **Live trading.** `EXECUTION_ALLOW_LIVE` stays unset. Flipping it is its own reviewed change with
  its own checklist.
- **Trailing stops / bracket orders.** The charter requires an exit be *recorded*; automating its
  placement is later work.

---

## 5. What must be rewritten when this ships

Not optional, and listed because this project's recurring defect is documentation that outlives its
truth:

- `SECURITY.md` — "the realistic worst case is disclosure of holdings, not trading" becomes false.
- `docs/AUTH_THREAT_MODEL.md` §1 — the value of a session changes; the threat model is keyed to a
  read-only dashboard.
- `src/alpaca.py` docstring — "It does not place orders. This module reads." stays true only if
  execution genuinely lives elsewhere.
- `README.md`, `PROJECT.md` — both describe a read-only monitor.

---

## 6. Decisions (resolved 2026-08-17)

1. **Who may execute — BOTH operators.** Either may preview, arm, confirm and disarm. The audit
   records which one, per action; there is no privilege tier and none is planned. If that ever
   changes it is a schema question, which is why it was settled before the schema was written.

2. **Order types — DEFERRED by the owner ("anything is fine for now").** Taken as: an explicit
   allow-list in config, defaulting to **limit-only**.

   "Anything" is not implementable as stated, and the default matters more than it looks. Limit-only
   is a strict SUBSET of limit-plus-market, so adding market later is additive and backward
   compatible; shipping permissive and restricting later breaks whatever started relying on it.
   A market order also silently delegates the price decision to the book, which on a thin name is
   the difference between the preview an owner approved and the fill they got — and the preview is
   the entire control in this design.

   Flipping `EXECUTION_ORDER_TYPES` to include `market` is a config change, not a rebuild. The
   decision is deferred, not foreclosed.

3. **Arming — ARMED WINDOW, per-order confirmation still required.** The window is convenience; the
   confirmation is the control. 15-minute TTL, visible countdown, instant ungated disarm.

4. **Paper carries the same ceremony as live — YES.** No divergent path. The live path must never be
   the one that has never been rehearsed.
