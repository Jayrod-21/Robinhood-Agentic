"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, Gavel, LayoutDashboard, ListFilter } from "lucide-react";
import { cn } from "@/lib/format";

const NAV = [
  { href: "/", label: "Portfolio", icon: LayoutDashboard },
  { href: "/scan", label: "Scan", icon: ListFilter },
  { href: "/pipeline", label: "Pipeline", icon: Activity },
  { href: "/debate", label: "Debate", icon: Gavel },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-ink-800 bg-ink-900/50 px-3 py-5 md:flex">
        <div className="px-2">
          <div className="font-serif text-xl text-zinc-100">Agentic</div>
          <div className="text-[11px] uppercase tracking-[0.2em] text-brass">Robinhood · ••••4025</div>
        </div>
        <nav className="mt-8 flex flex-col gap-1">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                  active ? "bg-ink-800 text-zinc-100" : "text-zinc-400 hover:bg-ink-850 hover:text-zinc-200",
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto px-2 text-[11px] leading-relaxed text-zinc-600">
          Read-only monitor. Live debates cost API tokens. Trades execute via the in-session agent.
        </div>
      </aside>
      <main className="flex-1 overflow-x-hidden px-4 py-6 sm:px-8">{children}</main>
    </div>
  );
}

export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: React.ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="font-serif text-2xl text-zinc-100">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-zinc-500">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}
