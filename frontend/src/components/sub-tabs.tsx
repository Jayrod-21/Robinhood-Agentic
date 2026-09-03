"use client";

// Sub-navigation inside a consolidated section.
//
// WHY THIS EXISTS
//     The sidebar carried thirteen entries, several of which were sub-data of another page wearing
//     a tab of its own — Fundamentals is the drill-down of a Scan survivor, Learning and
//     Calibration are two cuts of the same scored judgments. Thirteen top-level choices is not a
//     menu, it is a search problem.
//
//     The app already had the right pattern and used it twice: /position/[symbol] and
//     /debate/[id] are real pages reached by clicking a row, not by a nav entry. This generalises
//     that — a section owns a theme, and its parts are routes underneath it.
//
// REAL ROUTES, NOT CLIENT STATE
//     Each sub-tab is its own URL, so it can be linked, bookmarked, opened in a tab and returned
//     to from a drill-down. Sub-tabs held in useState would have made every one of those
//     impossible while looking identical on screen.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/format";

export interface SubTab {
  href: string;
  label: string;
}

export function SubTabs({ tabs }: { tabs: SubTab[] }) {
  const pathname = usePathname();
  return (
    <nav className="mb-5 flex flex-wrap gap-1 border-b border-ink-800" aria-label="Section">
      {tabs.map(({ href, label }) => {
        // Exact match only. A prefix test would light up the section root on every child route,
        // so two tabs would read as active at once.
        const active = pathname === href;
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm transition-colors",
              active
                ? "border-brass text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-200",
            )}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
