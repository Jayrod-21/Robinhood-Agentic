"use client";

// The account switcher: a dropdown (Portfolio header) to switch among the named Alpaca accounts.
// No dropdown primitive exists in the app, so this is a small self-contained one: click-outside +
// Escape to close, aria-expanded/haspopup, menuitemradio rows. Hidden entirely when the accounts
// list isn't available (there is nothing to switch between).

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Wallet } from "lucide-react";
import { cn } from "@/lib/format";
import { useAccount } from "@/components/account-context";

export function AccountSwitcher() {
  const { accounts, selected, setSelectedId, loading } = useAccount();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (loading || accounts.length === 0) return null;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Switch account"
        className="inline-flex items-center gap-2 rounded-lg border border-ink-700 bg-ink-800 px-3 py-2 text-sm text-zinc-200 transition-colors hover:bg-ink-700"
      >
        <Wallet className="h-4 w-4 text-brass" />
        <span className="max-w-[11rem] truncate">{selected?.name ?? "Select account"}</span>
        <ChevronDown className={cn("h-4 w-4 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div role="menu" className="absolute right-0 z-30 mt-1.5 w-72 rounded-lg border border-ink-700 bg-ink-900 py-1 shadow-xl">
          {accounts.map((a) => {
            const active = a.id === selected?.id;
            return (
              <button
                key={a.id}
                role="menuitemradio"
                aria-checked={active}
                onClick={() => {
                  setSelectedId(a.id);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-ink-800",
                  active && "bg-ink-850",
                )}
              >
                <Check className={cn("mt-0.5 h-4 w-4 shrink-0", active ? "text-gain" : "text-transparent")} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-zinc-100">{a.name}</span>
                </span>
                <span className={cn("mt-0.5 shrink-0 text-[10px] uppercase tracking-wide", a.is_paper ? "text-brass" : "text-loss")}>
                  {a.is_paper ? "paper" : "live"}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
