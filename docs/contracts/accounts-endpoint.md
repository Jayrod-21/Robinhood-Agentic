# Contract: `GET /api/accounts` + `account_id` on the account-reading routes

Feeds the account switcher (`frontend/src/components/account-switcher.tsx`, rendered in the Portfolio
header) and the global selection state (`frontend/src/components/account-context.tsx`). Built and
rendering today against a dev fixture (`NEXT_PUBLIC_ACCOUNTS_MOCK=1`, see `frontend/src/lib/accounts.ts`).
**Read-only.** TypeScript interfaces in `frontend/src/lib/accounts.ts` are the source of truth.

The app has always been single account (one env-based Alpaca key pair). This adds a set of named
accounts the operator can switch between, each its own Alpaca paper account running a different
strategy. Joe's five: AI Agentic Debate, Special Sprinkle Sauce, Debate B (variant under test), ML
Lab A, ML Lab B.

## `GET /api/accounts`

```jsonc
{
  "accounts": [
    { "id": "agentic-debate", "name": "AI Agentic Debate", "broker_env": "alpaca-paper", "strategy": "10-agent jury debate" },
    { "id": "sprinkle-sauce", "name": "Special Sprinkle Sauce", "broker_env": "alpaca-paper", "strategy": "5-tier screen" }
  ],
  "default_id": "agentic-debate"
}
```

- `id` is a stable slug the backend keys credentials and snapshots on, never the raw account number.
- `broker_env` is `alpaca-paper` or `alpaca-live` (the switcher badges live accounts differently).
- `default_id` is the account served when the request carries no `account_id` and the operator has no
  stored preference.

## The `account_id` param on account-reading routes

The frontend threads the selected account through as `?account_id=<id>` (see `withAccount()` in
`account-context.tsx`). The account-scoped routes should accept it and default to `default_id` when it
is absent:

- `GET /api/account?account_id=<id>` (wired on the Portfolio page today)
- and, as they get switched-account support: `/api/reconciliation`, `/api/position/{symbol}`,
  `/api/performance`, `/api/calibration`, `/api/data-trust`.

An unknown `account_id` should `404` (not silently serve the default), so a stale bookmark surfaces
rather than quietly showing the wrong account.

## Backend work (per the plan, this is Jared's)

- Move Alpaca credentials from the bare env pair to per-account. `src/alpaca.py::load_credentials()`
  already accepts explicit `key_id`/`secret`/`base_url`, and `AlpacaClient` takes them in its
  constructor, so this is additive: either `ALPACA_API_KEY_ID_<slug>` env pairs or a small profiles
  table mapping `id -> creds`.
- Re-key the module-level snapshot cache in `backend/app/services/broker.py` by `account_id`. Today
  `_cache` is a single global tuple with no account dimension; that is the one hard single-account
  assumption in the codebase.
- Joe supplies the five Alpaca paper key pairs.

## Degradation

- `GET /api/accounts` unavailable or not built (`404`/`503`) → the context resolves to no accounts and
  the switcher **hides entirely** (there is nothing to switch between); the app behaves exactly as the
  current single-account build. So shipping this frontend ahead of the backend is safe.

## Honesty note

`broker_env` is surfaced (a `live` account is badged in the loss color), so an operator can never
confuse a paper account with a live one at the moment they switch. When any account becomes
`alpaca-live`, that badge is the last cheap warning before real money.

## Frontend done / handoff

- [x] `GET /api/accounts` typed + fixture (`NEXT_PUBLIC_ACCOUNTS_MOCK=1`, registered in `ANY_MOCK`)
- [x] Global `AccountProvider` (localStorage-persisted selection) wrapping the app; `useAccount()` + `withAccount()`
- [x] Switcher dropdown in the Portfolio header; Portfolio `/api/account` fetch is account-scoped
- [ ] **backend:** implement `GET /api/accounts` + `account_id` on the account routes; per-account creds; re-key the broker cache
- [ ] **frontend follow-up:** thread `account_id` through the remaining account-scoped pages (reconciliation, position, performance, calibration, data-trust) once the backend accepts it
