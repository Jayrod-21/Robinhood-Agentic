# Contract: `GET /api/accounts` + `account_id` on the account-reading routes

Feeds the account switcher (`frontend/src/components/account-switcher.tsx`, rendered in the Portfolio
header) and the global selection state (`frontend/src/components/account-context.tsx`). **Read-only.**

**As-built.** The backend landed in PR #109 (`backend/app/routers/accounts.py` +
`backend/app/services/accounts.py`); this doc was reconciled to match it, and the frontend types in
`frontend/src/lib/accounts.ts` now mirror the live shape rather than the original slug-based sketch.

The app was single-account (one env-based Alpaca key pair). This adds a set of named accounts the
operator can switch between, each its own Alpaca account. Joe's five: AI Agentic Debate, Special
Sprinkle Sauce, Debate B (variant under test), ML Lab A, ML Lab B.

## `GET /api/accounts`

```jsonc
{
  "meta": {
    "count": 5,
    "default_account_id": 1,   // account served when no account_id is given and no stored preference
    "all_paper": true          // true when every configured account points at a paper endpoint
  },
  "accounts": [
    { "id": 1, "name": "AI Agentic Debate", "is_paper": true },
    { "id": 2, "name": "Special Sprinkle Sauce", "is_paper": true }
  ]
}
```

- `id` is the **integer profile number** (1..9), the index of the `ALPACA_ACCOUNT_<N>_*` env group.
  Never a slug, and never the key id (that is half a credential; the list carries no key material).
- `is_paper` is decided by the account's base URL on the backend (`"paper-api" in base_url`), so the
  list and the trading client can never disagree about which endpoint an account points at.
- The frontend coerces `id` to a string app-side (`toAccounts()` in `lib/accounts.ts`) so localStorage
  keys, the `account_id` query param, and equality checks are all one type.

## The `account_id` param on account-reading routes

The frontend threads the selected account through as `?account_id=<id>` (see `withAccount()` in
`account-context.tsx`). Omitting it serves account 1. Accepted on:

- `GET /api/account?account_id=<id>` (wired on the Portfolio page today)
- `GET /api/reconciliation`, `GET /api/position/{symbol}`, `GET /api/data-trust`,
  `GET /api/market-context`

An `account_id` that is not configured **refuses** rather than falling back to the snapshot file:
that file holds one account's positions, and serving it for another id would be the exact
one-book-under-another's-name substitution the registry exists to prevent.

## Backend (done, Jared)

- Credentials are per-account in the environment: `ALPACA_ACCOUNT_<N>_NAME / _KEY_ID / _SECRET_KEY /
  _BASE_URL`, N in 1..9. Account 1 falls back to the bare `ALPACA_API_KEY_ID` / `_SECRET_KEY` /
  `ALPACA_BASE_URL`, so an existing single-account deployment keeps working with no config change.
- The broker snapshot cache is keyed by `account_id` (was a single module-level tuple).
- **On Joe:** supply the five Alpaca paper key pairs to populate accounts 2..5 (1 already works).

## Degradation

- `GET /api/accounts` unavailable (`404`/`503`) → the context resolves to no accounts and the switcher
  **hides entirely** (nothing to switch between); the app behaves as the single-account build.

## Honesty note

`is_paper` drives a per-row badge: a paper account is badged in the brass color, a live account in the
loss color. An operator can never confuse a paper account with a live one at the moment they switch,
and `meta.all_paper` going false is a fact the UI surfaces rather than one learned by accident.

## Frontend done / handoff

- [x] `GET /api/accounts` typed to the live shape (`meta` + `{id, name, is_paper}`), fixture matches
      (`NEXT_PUBLIC_ACCOUNTS_MOCK=1`, registered in `ANY_MOCK`)
- [x] Global `AccountProvider` (localStorage-persisted selection) wrapping the app; `useAccount()` + `withAccount()`
- [x] Switcher dropdown in the Portfolio header, badge driven by `is_paper`; Portfolio `/api/account` fetch is account-scoped
- [ ] **frontend follow-up:** thread `account_id` through the remaining account-scoped pages
      (reconciliation, position, data-trust, market-context) now that the backend accepts it
