# Per-account slates

A slate is a claim about what **one specific book** should hold. This directory is where that claim
lives for accounts other than the first.

| Account | Slate file | Read by |
|---------|-----------|---------|
| 1 | `../SLATE.md` (or `account-1.md`, which wins if present) | `/api/reconciliation`, the cycle preflight |
| N | `account-N.md` | same, when `?account_id=N` |
| any, absent | — | reports "no documented slate", diffs nothing |

## The rule that matters

**Reconciliation never falls back to another account's slate.** Before `slate_path_for` existed,
`/api/reconciliation` accepted an `account_id` and then read `docs/SLATE.md` regardless. With one
account that is invisible; with five it means the ML-testing book is measured against the agentic
debate book's Reframe-Barbell targets, reports every row as a finding, and stays permanently red.

An alarm that is always on is one an operator learns to skim past — and then the real desync arrives
looking exactly the same. So an account with no slate reports **"no documented slate"** and diffs
nothing. That is a normal state. A testing book is not supposed to have targets.

## Writing one

Copy the allocation table's shape from `../SLATE.md`. The parser
(`backend/app/services/slate.py::load_slate`) reads rows matching:

```
| **TICKER** | 22 | $22,000 | Role | Why that size |
```

`CASH` is read as the cash target and never as a position. Two optional lines are parsed out of the
prose for the guardrail checks — a hard stop and a trim multiple — see `load_sizing_rules`.

A slate with no parseable rows is an error, not an empty slate: `/api/reconciliation` answers 503
rather than reporting that the broker holds nothing the slate documents. Those are opposite
conclusions and must not render the same way.

## Which accounts exist

`GET /api/accounts` is the source of truth, populated from `ALPACA_ACCOUNT_<N>_*` environment
variables (`backend/app/services/accounts.py`). This directory does not need an entry per account and
should not grow placeholder files for accounts that have no targets — an empty slate and no slate
are different states, and only one of them is honest about a testing book.
