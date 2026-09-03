# Per-account slates

A slate is a claim about what **one specific book** should hold. This directory is where that claim
lives for accounts other than the first.

| Account | Slate file | Read by |
|---------|-----------|---------|
| 1 | `../SLATE.md` (or `account-1.md`, which wins if present) | `/api/reconciliation`, the cycle preflight |
| N | `account-N.md` | same, when `?account_id=N` |
| any, absent | — | reports "no documented slate", diffs nothing |
| any, retired | the file, marked `NOT IN FORCE` | reports "no slate in force", diffs nothing |

## The rule that matters

**Reconciliation never falls back to another account's slate.** Before `slate_path_for` existed,
`/api/reconciliation` accepted an `account_id` and then read `docs/SLATE.md` regardless. With one
account that is invisible; with five it means the ML-testing book is measured against the agentic
debate book's Reframe-Barbell targets, reports every row as a finding, and stays permanently red.

An alarm that is always on is one an operator learns to skim past — and then the real desync arrives
looking exactly the same. So an account with no slate reports **"no documented slate"** and diffs
nothing. That is a normal state. A testing book is not supposed to have targets.

## Retiring one without deleting it

A slate can be a document and not a target. Put this line near the top of the file:

```
> **Slate status: NOT IN FORCE** — why, in one clause.
```

`slate_status` parses it; `load_governing_slate` then returns **no targets** to anybody, so
reconciliation, the position page and market-context relevance all stop applying weights that no
longer describe the book. The table stays exactly where it is, because it is the written record of a
real debate — retiring is not deleting. Remove the line to put the slate back in force.

**Absence means in force.** Every slate written before this existed governs its account unchanged;
the status is read, never inferred from a date. A stale-looking slate that IS in force and a
current-looking one that is NOT are precisely the two cases a heuristic gets backwards.

**Why this exists.** `docs/SLATE.md`'s weights were decided against a $100 Robinhood book. The
account of record moved to an Alpaca paper book on 2026-08-17, whose fifteen positions were placed
by `bin/seed_paper_book.py` — an owner seeding an equal-dollar basket so the marking job had
something to value, which that script is explicit is not the agentic loop deciding anything.
Reconciling the second against the first printed "0 matched · 5 drifted · 3 missing · 10
undocumented · 2 guardrail breach(es)" at the top of every morning report for weeks. Not one of
those was a portfolio finding. This is the same failure the rule below prevents between accounts,
arriving instead through time — and it costs the same thing: an operator who reads OUT OF SYNC every
morning stops reading it.

Give a reason. A bare `NOT IN FORCE` is how a temporary silence becomes a permanent one; a test in
`backend/tests/test_slate_not_in_force.py` fails if `docs/SLATE.md` is retired without one.

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
