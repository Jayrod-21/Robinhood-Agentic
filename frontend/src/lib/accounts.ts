// Types plus a dev-only fixture for the multi-account switcher. The app was single-account (one
// env-based Alpaca key pair); this adds several named accounts the operator can switch between,
// each its own Alpaca account running a different strategy.
//
// Shapes here mirror the LIVE backend (backend/app/routers/accounts.py, merged): GET /api/accounts
// returns `{ meta, accounts }`, each account is `{ id, name, is_paper }`, and the ids are the
// integer profile numbers (1..9), never a slug and never any key material. Contract:
// docs/contracts/accounts-endpoint.md.

/** One account as it comes off the wire. `id` is the integer profile number; `is_paper` is decided
 *  by the account's base URL on the backend, so it can never disagree with the client that trades. */
export interface AccountWire {
  id: number;
  name: string;
  is_paper: boolean;
}

export interface AccountsResponse {
  meta: {
    count: number;
    /** Which account to select when the operator has no stored preference. */
    default_account_id: number;
    /** True when every configured account points at a paper endpoint. */
    all_paper: boolean;
  };
  accounts: AccountWire[];
}

/** App-side account. Same fields as the wire, but `id` is coerced to a string so localStorage keys,
 *  the `account_id` query param, and equality checks are all one type. */
export interface Account {
  id: string;
  name: string;
  is_paper: boolean;
}

/** Coerce the wire response (integer ids) into app accounts (string ids). */
export function toAccounts(resp: AccountsResponse | undefined): Account[] {
  return (resp?.accounts ?? []).map((a) => ({ id: String(a.id), name: a.name, is_paper: a.is_paper }));
}

/** The backend's default account id as a string, or null before the response is in. */
export function defaultAccountId(resp: AccountsResponse | undefined): string | null {
  return resp?.meta ? String(resp.meta.default_account_id) : null;
}

// ── Dev fixture ──────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_ACCOUNTS_MOCK=1); the default hits GET /api/accounts. Matches the wire
// shape exactly (integer ids, is_paper, meta) so the switcher behaves the same mocked or live. Five
// accounts per Joe's plan: two live strategies, a debate variant to test against, two for ML work.
export const ACCOUNTS_MOCK = process.env.NEXT_PUBLIC_ACCOUNTS_MOCK === "1";

export const MOCK_ACCOUNTS: AccountsResponse = {
  meta: { count: 5, default_account_id: 1, all_paper: true },
  accounts: [
    { id: 1, name: "AI Agentic Debate", is_paper: true },
    { id: 2, name: "Special Sprinkle Sauce", is_paper: true },
    { id: 3, name: "Debate B", is_paper: true },
    { id: 4, name: "ML Lab A", is_paper: true },
    { id: 5, name: "ML Lab B", is_paper: true },
  ],
};
