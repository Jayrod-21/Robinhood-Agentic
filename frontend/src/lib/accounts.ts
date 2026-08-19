// Types plus a dev-only fixture for the multi-account switcher. The app has always been single
// account (one env-based Alpaca key pair); this adds a notion of several named accounts the operator
// can switch between, each its own Alpaca paper account running a different strategy.
//
// Frontend owns the selector + the selected-account state; the backend (Jared) adds GET /api/accounts
// and an account_id param on the account-reading routes. Contract: docs/contracts/accounts-endpoint.md.

export type BrokerEnv = "alpaca-paper" | "alpaca-live";

export interface Account {
  /** Stable slug the backend keys credentials/snapshots on (never the raw account number). */
  id: string;
  name: string;
  broker_env: BrokerEnv;
  /** One-line strategy tag for the dropdown; null when none. */
  strategy: string | null;
}

export interface AccountsResponse {
  accounts: Account[];
  /** Which account to select when the operator has no stored preference. */
  default_id: string;
}

// ── Dev fixture ──────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_ACCOUNTS_MOCK=1); the default hits GET /api/accounts. The five accounts
// match Joe's plan: two live strategies, a debate variant to test against, and two for ML/algo work.
export const ACCOUNTS_MOCK = process.env.NEXT_PUBLIC_ACCOUNTS_MOCK === "1";

export const MOCK_ACCOUNTS: AccountsResponse = {
  default_id: "agentic-debate",
  accounts: [
    { id: "agentic-debate", name: "AI Agentic Debate", broker_env: "alpaca-paper", strategy: "10-agent jury debate" },
    { id: "sprinkle-sauce", name: "Special Sprinkle Sauce", broker_env: "alpaca-paper", strategy: "5-tier screen" },
    { id: "debate-b", name: "Debate B", broker_env: "alpaca-paper", strategy: "debate variant, under test" },
    { id: "ml-lab-a", name: "ML Lab A", broker_env: "alpaca-paper", strategy: "model testing" },
    { id: "ml-lab-b", name: "ML Lab B", broker_env: "alpaca-paper", strategy: "algo testing" },
  ],
};
