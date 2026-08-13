// Shared fundamentals display — used by the Scan expandable rows and the debate detail page.
// Formatting helpers live here (not lib/format.ts) so both consumers pull one import.

import { cn } from "@/lib/format";
import type { FundamentalsData } from "@/lib/types";

/** Compact dollar formatting for big figures: $2.21T / $719.16B / $425.83. */
export function compactUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  return `$${n.toFixed(2)}`;
}

/** Ratio-style number (PEG, P/E): 2 decimals or an em dash. */
export function ratio(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toFixed(2);
}

/** A yfinance fraction (0.62) rendered as a percentage (62.0%). */
export function fractionPct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(1)}%`;
}

/** A value already stored in percentage units (32.5 == 32.5%). */
export function rawPct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${n.toFixed(1)}%`;
}

function Item({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className={cn("mt-0.5 text-sm tnum text-zinc-200", valueClass)}>{value}</div>
    </div>
  );
}

/** Dense grid of every fundamental the backend surfaces. Missing values render as an em dash so
 *  a sparse yfinance payload degrades visibly rather than shifting the layout. */
export function FundamentalsGrid({ data, className }: { data: FundamentalsData; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-5", className)}>
      <Item label="Price" value={data.price != null ? `$${data.price.toFixed(2)}` : "—"} />
      <Item label="Mkt cap" value={compactUsd(data.market_cap)} />
      <Item label="PEG" value={ratio(data.peg)} />
      <Item label="FCF yield" value={rawPct(data.fcf_yield)} valueClass={data.fcf_yield != null && data.fcf_yield >= 3 ? "text-gain" : undefined} />
      <Item label="Trailing P/E" value={ratio(data.trailing_pe)} />
      <Item label="Forward P/E" value={ratio(data.forward_pe)} />
      <Item label="Gross margin" value={fractionPct(data.gross_margin)} />
      <Item label="Rev growth" value={fractionPct(data.revenue_growth)} valueClass={data.revenue_growth != null ? (data.revenue_growth >= 0 ? "text-gain" : "text-loss") : undefined} />
      <Item label="Sector" value={data.sector ?? "—"} />
      <Item label="Industry" value={data.industry ?? "—"} />
    </div>
  );
}
