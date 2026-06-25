import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function usd(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: dp, maximumFractionDigits: dp });
}

export function pct(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined) return "—";
  const s = n >= 0 ? "+" : "";
  return `${s}${n.toFixed(dp)}%`;
}

export function plColor(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-zinc-400";
  if (n > 0) return "text-gain";
  if (n < 0) return "text-loss";
  return "text-flat";
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
