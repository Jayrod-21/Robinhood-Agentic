"use client";

import Link from "next/link";
import useSWR from "swr";
import { AlertTriangle } from "lucide-react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { PageHeader } from "@/components/shell";
import { Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { AccountSwitcher } from "@/components/account-switcher";
import { useAccount, withAccount } from "@/components/account-context";
import { ReconciliationSection } from "@/components/views/reconciliation-section";
import { fetcher } from "@/lib/api";
import { ago, cn, pct, plColor, usd } from "@/lib/format";
import type { AccountView } from "@/lib/types";

const DONUT = ["#e0b34d", "#34d399", "#60a5fa", "#f472b6", "#a78bfa", "#fb923c", "#22d3ee", "#facc15", "#94a3b8"];

// weight_account_pct landed with issue #21: the charter's ~25%/name cap is stated against
// ACCOUNT value (equity + cash), so both weight bases are exposed and labelled. The shared
// types file (src/lib/types.ts) is owned by another workstream; widen locally until it
// mirrors the backend PositionView. Optional so an older backend degrades to "—".

const fmtWeight = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)}%`);

// Human names for the snapshot `source` values the backend emits (src/alpaca.py, services/snapshot.py).
// Rendered from the live payload — never hard-coded — so the page cannot claim a broker the data did
// not actually come from (Alpaca is preferred; the Robinhood file only serves when creds are absent).
const SOURCE_LABELS: Record<string, string> = {
  "alpaca-paper": "the Alpaca paper account",
  "alpaca-live": "the Alpaca live account",
  "robinhood-mcp": "the saved Robinhood snapshot",
};
const sourceLabel = (source: string) => SOURCE_LABELS[source] ?? source;

export default function PortfolioPage() {
  const { selectedId } = useAccount();
  // Account-scoped: the fetch re-keys when the operator switches accounts, so SWR fetches the newly
  // selected account and the page swaps to it. Falls back to the backend default when none is selected.
  const { data, error, isLoading } = useSWR<AccountView>(withAccount("/api/account", selectedId), fetcher, { refreshInterval: 10_000 });
  const plClass = plColor(data?.total_unrealized_pl);
  const donutData =
    data?.positions
      .filter((p) => p.priced && p.market_value)
      .map((p) => ({ name: p.symbol, value: p.market_value as number }))
      .concat(data.cash > 0 ? [{ name: "CASH", value: data.cash }] : []) ?? [];

  return (
    <div>
      <PageHeader
        title="Portfolio"
        subtitle={data ? `${data.nickname ?? "Account"} ${data.account_masked} · read ${ago(data.generated_at)}` : "Live Agentic account"}
        right={<AccountSwitcher />}
      />

      {error && (
        <Card className="mb-6 border-loss/40">
          <CardBody className="flex items-center gap-3 pt-5 text-sm text-loss">
            <AlertTriangle className="h-4 w-4" /> {String(error.message ?? error)}
          </CardBody>
        </Card>
      )}

      {data?.stale_prices && (
        <div className="mb-4 flex items-center gap-2 text-xs text-flat">
          <AlertTriangle className="h-3.5 w-3.5" /> Some positions could not be priced live; their P&amp;L is omitted.
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Total value" value={usd(data?.live_total_value)} sub={`equity ${usd(data?.live_equity_value)}`} />
        <StatCard label="Unrealized P&L" value={usd(data?.total_unrealized_pl)} sub={pct(data?.total_unrealized_pl_pct)} valueClass={plClass} />
        <StatCard label="Cash" value={usd(data?.cash)} sub={`buying power ${usd(data?.buying_power)}`} />
        <StatCard label="Cost basis" value={usd(data?.total_cost_basis)} sub={`${data?.positions.length ?? 0} positions`} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_320px]">
        <Card>
          <CardHeader>
            <CardTitle>Positions</CardTitle>
          </CardHeader>
          <CardBody className="overflow-x-auto p-0">
            {isLoading && !data ? (
              <div className="flex items-center gap-2 px-5 py-8 text-sm text-zinc-500">
                <Spinner /> Loading account…
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                    <th className="px-5 py-2 font-medium">Symbol</th>
                    <th className="px-3 py-2 text-right font-medium">Price</th>
                    <th className="px-3 py-2 text-right font-medium">Avg cost</th>
                    <th className="px-3 py-2 text-right font-medium">Mkt value</th>
                    <th className="px-3 py-2 text-right font-medium">P&L</th>
                    <th className="px-3 py-2 text-right font-medium" title="Share of account value (equity + cash) — the basis of the ~25%-per-name position cap">
                      Wt % acct
                    </th>
                    <th className="px-5 py-2 text-right font-medium" title="Share of invested equity only (excludes cash)">
                      Wt % equity
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data?.positions.map((p) => (
                    <tr key={p.symbol} className="border-b border-ink-850 last:border-0 hover:bg-ink-850/50">
                      <td className="px-5 py-2.5 font-medium">
                        <Link href={`/position/${encodeURIComponent(p.symbol)}`} className="text-zinc-100 transition-colors hover:text-brass">
                          {p.symbol}
                        </Link>
                      </td>
                      <td className="px-3 py-2.5 text-right tnum text-zinc-300">{usd(p.current_price)}</td>
                      <td className="px-3 py-2.5 text-right tnum text-zinc-400">{usd(p.average_buy_price)}</td>
                      <td className="px-3 py-2.5 text-right tnum text-zinc-200">{usd(p.market_value)}</td>
                      <td className={cn("px-3 py-2.5 text-right tnum", plColor(p.unrealized_pl))}>
                        {usd(p.unrealized_pl)} <span className="text-xs">({pct(p.unrealized_pl_pct)})</span>
                      </td>
                      <td className="px-3 py-2.5 text-right tnum text-zinc-200">{fmtWeight(p.weight_account_pct)}</td>
                      <td className="px-5 py-2.5 text-right tnum text-zinc-500">{fmtWeight(p.weight_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Allocation</CardTitle>
          </CardHeader>
          <CardBody>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={donutData} dataKey="value" nameKey="name" innerRadius={52} outerRadius={84} paddingAngle={2} stroke="none">
                    {donutData.map((_, i) => (
                      <Cell key={i} fill={DONUT[i % DONUT.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{ background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 }}
                    formatter={(v: number, n) => [usd(v), n as string]}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-400">
              {donutData.map((d, i) => (
                <span key={d.name} className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full" style={{ background: DONUT[i % DONUT.length] }} />
                  {d.name}
                </span>
              ))}
            </div>
            <p className="mt-4 text-[11px] leading-relaxed text-zinc-600">
              Positions &amp; cost basis come from {data ? sourceLabel(data.source) : "the connected account"}; prices
              and P&amp;L refresh live from FMP.
              {data?.source === "robinhood-mcp" && (
                <>
                  {" "}
                  This is the saved fallback file, not a broker read — Alpaca credentials are absent
                  or the broker is unreachable.
                </>
              )}
            </p>
          </CardBody>
        </Card>
      </div>

      {/* Both halves of "what do we hold": the book itself, then whether it is the book we said we
          would hold. These were two top-level tabs answering one question. */}
      <ReconciliationSection />
    </div>
  );
}
