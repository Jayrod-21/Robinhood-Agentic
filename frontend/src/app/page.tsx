"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { RefreshCw, AlertTriangle } from "lucide-react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { fetcher, postJSON } from "@/lib/api";
import { ago, cn, pct, plColor, usd } from "@/lib/format";
import type { AccountView, RefreshStatus } from "@/lib/types";

const DONUT = ["#e0b34d", "#34d399", "#60a5fa", "#f472b6", "#a78bfa", "#fb923c", "#22d3ee", "#facc15", "#94a3b8"];

// weight_account_pct landed with issue #21: the charter's ~25%/name cap is stated against
// ACCOUNT value (equity + cash), so both weight bases are exposed and labelled. The shared
// types file (src/lib/types.ts) is owned by another workstream; widen locally until it
// mirrors the backend PositionView. Optional so an older backend degrades to "—".

const fmtWeight = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)}%`);

export default function PortfolioPage() {
  const { data, error, isLoading } = useSWR<AccountView>("/api/account", fetcher, { refreshInterval: 10_000 });
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const baselineTs = useRef<string | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function clearRefreshTimer() {
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }

  // Clear the fallback timer if it's still pending when the component unmounts.
  useEffect(() => clearRefreshTimer, []);

  // When a refresh is in flight, clear the spinner once the snapshot timestamp advances. Also cancel
  // the 4-min fallback timer so it can't fire setState after the component has unmounted.
  useEffect(() => {
    if (refreshing && data?.generated_at && baselineTs.current && data.generated_at !== baselineTs.current) {
      clearRefreshTimer();
      setRefreshing(false);
      setRefreshMsg("Updated just now");
    }
  }, [data?.generated_at, refreshing]);

  async function onRefresh() {
    try {
      baselineTs.current = data?.generated_at ?? null;
      setRefreshMsg(null);
      setRefreshing(true);
      const res = await postJSON<{ status: string; detail: string }>("/api/refresh", {});
      setRefreshMsg(res.detail);
      if (res.status === "cooldown") {
        setRefreshing(false);
        return;
      }
      // Stop spinning after 4 min even if the daemon never updated (e.g. not running). Stored in a
      // ref so the success effect / unmount can cancel it instead of letting it fire blindly.
      clearRefreshTimer();
      refreshTimer.current = setTimeout(() => {
        setRefreshing(false);
        refreshTimer.current = null;
      }, 240_000);
    } catch (e) {
      clearRefreshTimer();
      setRefreshing(false);
      setRefreshMsg(e instanceof Error ? e.message : "Refresh failed");
    }
  }

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
        subtitle={data ? `${data.nickname ?? "Account"} ${data.account_masked} · snapshot ${ago(data.generated_at)}` : "Live Agentic account"}
        right={
          <div className="flex items-center gap-3">
            {refreshMsg && <span className="text-xs text-zinc-500">{refreshMsg}</span>}
            <Button variant="brass" onClick={onRefresh} disabled={refreshing}>
              {refreshing ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : <RefreshCw className="h-4 w-4" />}
              {refreshing ? "Refreshing…" : "Refresh from Robinhood"}
            </Button>
          </div>
        }
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
                      <td className="px-5 py-2.5 font-medium text-zinc-100">{p.symbol}</td>
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
              Positions &amp; cost basis are the real Robinhood snapshot; prices and P&amp;L refresh live from yfinance.
              Click <Badge tone="neutral">Refresh from Robinhood</Badge> to re-pull holdings via the MCP bridge.
            </p>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
