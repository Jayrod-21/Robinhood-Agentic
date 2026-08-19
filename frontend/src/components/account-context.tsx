"use client";

// Global selected-account state. The switcher (Portfolio header) sets it; every account-scoped page
// reads `selectedId` to key its /api/... calls. Persisted to localStorage so a reload keeps the
// operator on the account they were looking at. Degrades cleanly: until the accounts list resolves
// (or if it never does), `selected` is null and callers fall back to the backend's default account.

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { ACCOUNTS_MOCK, MOCK_ACCOUNTS, type Account, type AccountsResponse } from "@/lib/accounts";

const STORAGE_KEY = "ww.selectedAccountId";

interface AccountContextValue {
  accounts: Account[];
  selectedId: string | null;
  selected: Account | null;
  setSelectedId: (id: string) => void;
  loading: boolean;
}

const AccountContext = createContext<AccountContextValue | null>(null);

export function AccountProvider({ children }: { children: ReactNode }) {
  const { data } = useSWR<AccountsResponse>(
    ACCOUNTS_MOCK ? null : "/api/accounts",
    fetcher,
    { shouldRetryOnError: false, revalidateOnFocus: false },
  );
  const resp = ACCOUNTS_MOCK ? MOCK_ACCOUNTS : data;
  const accounts = resp?.accounts ?? [];
  const [selectedId, setSelectedIdState] = useState<string | null>(null);

  // Hydrate the selection once the list is known. localStorage is read here (an effect), never in
  // initial state, so server and client render the same first paint. An unknown/stale stored id
  // falls back to the backend default, then the first account.
  useEffect(() => {
    if (!accounts.length) return;
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEY) : null;
    const valid = stored && accounts.some((a) => a.id === stored) ? stored : resp?.default_id ?? accounts[0].id;
    setSelectedIdState(valid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts.length, resp?.default_id]);

  const setSelectedId = (id: string) => {
    setSelectedIdState(id);
    if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, id);
  };

  const selected = accounts.find((a) => a.id === selectedId) ?? null;

  return (
    <AccountContext.Provider value={{ accounts, selectedId, selected, setSelectedId, loading: !resp }}>
      {children}
    </AccountContext.Provider>
  );
}

export function useAccount(): AccountContextValue {
  const ctx = useContext(AccountContext);
  if (!ctx) throw new Error("useAccount must be used within an AccountProvider");
  return ctx;
}

/** Append the selected account to an API path, so a page's SWR key is account-scoped. Returns the
 *  path unchanged when nothing is selected yet (the backend then serves its default account). */
export function withAccount(path: string, selectedId: string | null): string {
  if (!selectedId) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}account_id=${encodeURIComponent(selectedId)}`;
}
