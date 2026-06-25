import * as React from "react";
import { cn } from "@/lib/format";

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-xl border border-ink-800 bg-ink-900/70 shadow-sm", className)} {...props} />;
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 pt-4 pb-2", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn("text-sm font-medium tracking-wide text-zinc-300", className)} {...props} />;
}

export function CardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 pb-5", className)} {...props} />;
}

const badgeTones: Record<string, string> = {
  buy: "bg-gain/15 text-gain border-gain/30",
  sell: "bg-loss/15 text-loss border-loss/30",
  hold: "bg-flat/15 text-flat border-flat/30",
  escalated: "bg-fuchsia-500/15 text-fuchsia-300 border-fuchsia-500/30",
  neutral: "bg-ink-800 text-zinc-300 border-ink-700",
};

export function Badge({
  tone = "neutral",
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: keyof typeof badgeTones }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium tracking-wide",
        badgeTones[tone],
        className,
      )}
      {...props}
    />
  );
}

export function decisionTone(decision?: string | null): keyof typeof badgeTones {
  switch (decision) {
    case "BUY":
      return "buy";
    case "SELL":
      return "sell";
    case "HOLD":
      return "hold";
    case "ESCALATED":
      return "escalated";
    default:
      return "neutral";
  }
}

export function Button({
  className,
  variant = "default",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "ghost" | "brass" }) {
  const variants = {
    default: "bg-ink-800 hover:bg-ink-700 text-zinc-100 border border-ink-700",
    ghost: "hover:bg-ink-800 text-zinc-300",
    brass: "bg-brass/90 hover:bg-brass text-ink-950 font-semibold",
  };
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
        variants[variant],
        className,
      )}
      {...props}
    />
  );
}

export function StatCard({
  label,
  value,
  sub,
  valueClass,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  valueClass?: string;
}) {
  return (
    <Card className="px-5 py-4">
      <div className="text-xs uppercase tracking-widest text-zinc-500">{label}</div>
      <div className={cn("mt-1 text-2xl font-semibold tnum", valueClass)}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-zinc-500 tnum">{sub}</div>}
    </Card>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={cn("inline-block h-4 w-4 animate-spin rounded-full border-2 border-zinc-600 border-t-zinc-200", className)}
    />
  );
}
