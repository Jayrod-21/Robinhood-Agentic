"use client";

import { useState, type ReactNode } from "react";
import useSWR from "swr";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AlertTriangle, Database } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { cn, pct, plColor, usd } from "@/lib/format";
import { MOCK_PERFORMANCE, PERF_MOCK, type PerformanceResponse } from "@/lib/perf";

// Portfolio in brass (the house accent), benchmark in a cool blue so the two lines never rely on
// colour alone to be told apart — the legend labels them too.
const PORT_COLOR = "#e0b34d";
const BENCH_COLOR = "#60a5fa";

// Fractional (0.0123) → "+1.23%". `pct` already handles the sign + em-dash for null.
const fpct = (v: number | null | undefined, dp = 2) => (v == null ? "—" : pct(v * 100, dp));
// A bare ratio (Sharpe/Sortino/IR): two decimals, em-dash when undefined (n < 2), never a fake 0.
const ratio = (v: number | null | undefined, dp = 2) => (v == null ? "—" : v.toFixed(dp));

type ChartMode = "value" | "return";

export default function PerformancePage() {
  // Mock mode short-circuits the fetch entirely (SWR key = null) so a missing endpoint can't be
  // mistaken for an empty book. Default/production hits the real route.
  const { data, error, isLoading } = useSWR<PerformanceResponse>(
    PERF_MOCK ? null : "/api/performance",
    fetcher,
    { refreshInterval: 30_000 },
  );
  const [mode, setMode] = useState<ChartMode>("value");

  const resp = PERF_MOCK ? MOCK_PERFORMANCE : data;

  const header = (
    <PageHeader
      title="Performance"
      subtitle={
        resp
          ? `${resp.meta.benchmark_symbol ?? "no"} benchmark · since ${resp.meta.inception_date}` +
            (resp.meta.priced_through ? ` · priced through ${resp.meta.priced_through}` : "")
          : "Risk-adjusted track record"
      }
      right={PERF_MOCK ? <Badge tone="hold">MOCK DATA</Badge> : undefined}
    />
  );

  // A 503 here is the history layer's stated degradation contract (DB absent), not a crash — say so
  // plainly and keep it distinct from a real error.
  if (error) {
    const degraded = /\b503\b/.test(String(error.message ?? error));
    return (
      <div>
        {header}
        <Card className={cn("border-loss/40", degraded && "border-ink-700")}>
          <CardBody className="flex items-start gap-3 pt-5 text-sm">
            {degraded ? (
              <>
                <Database className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" />
                <span className="text-zinc-400">
                  The evaluation database isn&apos;t connected, so there&apos;s no marking history to
                  chart yet. This page reads the daily marks the valuation job writes; it fills in
                  once the DB is up.
                </span>
              </>
            ) : (
              <>
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-loss" />
                <span className="text-loss">{String(error.message ?? error)}</span>
              </>
            )}
          </CardBody>
        </Card>
      </div>
    );
  }

  if (isLoading || !resp) {
    return (
      <div>
        {header}
        <div className="flex items-center gap-2 px-1 py-8 text-sm text-zinc-500">
          <Spinner /> Loading track record…
        </div>
      </div>
    );
  }

  const { meta, equity_curve, metrics } = resp;

  if (equity_curve.length === 0) {
    return (
      <div>
        {header}
        <Card>
          <CardBody className="pt-6 text-sm text-zinc-500">
            No marks yet. The valuation job values the paper book once per trading day; the equity
            curve and risk metrics appear here after it has run at least twice.
          </CardBody>
        </Card>
      </div>
    );
  }

  // Rebase the benchmark to the book's starting value so the two lines answer the only question
  // that matters — "would I have done better in the index?" — on one dollar axis. In % mode both
  // start at 0 and we read the spread directly.
  const startValue = equity_curve[0]?.market_value ?? 100;
  const chartData = equity_curve.map((p) => ({
    date: p.trade_date,
    port:
      mode === "value"
        ? p.market_value
        : p.cumulative_return == null
          ? null
          : p.cumulative_return * 100,
    bench:
      p.benchmark_cumulative_return == null
        ? null
        : mode === "value"
          ? Number((startValue * (1 + p.benchmark_cumulative_return)).toFixed(2))
          : p.benchmark_cumulative_return * 100,
  }));

  const notRankable = metrics != null && !metrics.is_rankable;
  const alpha =
    metrics?.total_return != null && equity_curve.length
      ? metrics.total_return - (equity_curve[equity_curve.length - 1].benchmark_cumulative_return ?? 0)
      : null;

  return (
    <div>
      {header}

      {/* Honesty banners — the whole point of this project is not overstating what it knows. */}
      {notRankable && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-flat/30 bg-flat/10 px-3 py-2 text-xs text-flat">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            <span className="font-medium">Not yet rankable.</span> {metrics!.n_observations} of the{" "}
            {metrics!.min_n_for_ranking} marks needed to rank this book. The ratios below are shown
            for context, but the record is still too short to tell skill from luck.
          </span>
        </div>
      )}
      {meta.returns_basis === "price_only" && (
        <div className="mb-3 flex items-start gap-2 text-xs text-zinc-500">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-600" />
          <span>
            Returns are <span className="text-zinc-300">price-only</span> — dividends aren&apos;t
            loaded for this book or its benchmark, so these are not total returns.
          </span>
        </div>
      )}
      {meta.coverage != null && meta.coverage < 1 && (
        <div className="mb-3 flex items-start gap-2 text-xs text-zinc-500">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-600" />
          <span>
            Coverage {(meta.coverage * 100).toFixed(1)}% of expected trading days
            {meta.coverage_note ? ` — ${meta.coverage_note}` : ""}.
          </span>
        </div>
      )}

      {/* Headline four, mirroring the Portfolio page's stat row. */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="Total return"
          value={fpct(metrics?.total_return)}
          sub={`since ${meta.inception_date}`}
          valueClass={plColor(metrics?.total_return)}
        />
        <StatCard
          label={`vs ${meta.benchmark_symbol ?? "benchmark"}`}
          value={fpct(alpha)}
          sub="excess return"
          valueClass={plColor(alpha)}
        />
        <StatCard
          label="Max drawdown"
          value={fpct(metrics?.max_drawdown)}
          sub="peak-to-trough"
          valueClass={metrics?.max_drawdown ? "text-loss" : undefined}
        />
        <StatCard
          label="Sharpe"
          value={ratio(metrics?.sharpe)}
          sub={notRankable ? `n=${metrics?.n_observations} · unranked` : `Sortino ${ratio(metrics?.sortino)}`}
          valueClass={notRankable ? "text-zinc-400" : undefined}
        />
      </div>

      {/* Equity curve vs benchmark. */}
      <Card className="mt-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Equity curve{meta.benchmark_symbol ? ` vs ${meta.benchmark_symbol}` : ""}</CardTitle>
          <div className="flex gap-1">
            <Button variant={mode === "value" ? "brass" : "ghost"} className="px-2.5 py-1 text-xs" onClick={() => setMode("value")}>
              Value
            </Button>
            <Button variant={mode === "return" ? "brass" : "ghost"} className="px-2.5 py-1 text-xs" onClick={() => setMode("return")}>
              Return %
            </Button>
          </div>
        </CardHeader>
        <CardBody>
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 8, right: 8, bottom: 4, left: 4 }}>
                <CartesianGrid stroke="#23262d" vertical={false} />
                <XAxis
                  dataKey="date"
                  tick={{ fill: "#71717a", fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: "#2e323b" }}
                  minTickGap={40}
                  tickFormatter={(d: string) => d.slice(5)}
                />
                <YAxis
                  tick={{ fill: "#71717a", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  width={54}
                  domain={["auto", "auto"]}
                  tickFormatter={(v: number) => (mode === "value" ? usd(v, 0) : `${v.toFixed(0)}%`)}
                />
                {mode === "return" && <ReferenceLine y={0} stroke="#2e323b" />}
                <Tooltip
                  contentStyle={{ background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: "#a1a1aa" }}
                  formatter={(v: number, name) => [
                    mode === "value" ? usd(v) : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`,
                    name === "port" ? "Portfolio" : (meta.benchmark_symbol ?? "Benchmark"),
                  ]}
                />
                <Line type="monotone" dataKey="port" stroke={PORT_COLOR} strokeWidth={2} dot={false} connectNulls name="port" />
                <Line
                  type="monotone"
                  dataKey="bench"
                  stroke={BENCH_COLOR}
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                  dot={false}
                  connectNulls
                  name="bench"
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-400">
            <span className="inline-flex items-center gap-1.5">
              <span className="h-2 w-4 rounded-full" style={{ background: PORT_COLOR }} /> Portfolio
            </span>
            {meta.benchmark_symbol && (
              <span className="inline-flex items-center gap-1.5">
                <span className="h-0 w-4 border-t-2 border-dashed" style={{ borderColor: BENCH_COLOR }} /> {meta.benchmark_symbol}
              </span>
            )}
          </div>
        </CardBody>
      </Card>

      {/* Full risk-adjusted scorecard. */}
      <Card className={cn("mt-4", notRankable && "opacity-90")}>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Risk-adjusted metrics</CardTitle>
          {metrics && (
            <span className="text-xs text-zinc-500">
              {metrics.walk_forward === "live" ? "live forward marking" : metrics.walk_forward} ·{" "}
              n={metrics.n_observations} ·{" "}
              {metrics.is_rankable ? <span className="text-gain">rankable</span> : <span className="text-flat">unranked</span>} · rf{" "}
              {(metrics.risk_free_annual * 100).toFixed(1)}%
            </span>
          )}
        </CardHeader>
        <CardBody>
          {metrics ? (
            <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
              <Metric label="Sharpe" value={ratio(metrics.sharpe)} hint="excess return per unit of total volatility" />
              <Metric label="Sortino" value={ratio(metrics.sortino)} hint="excess return per unit of downside volatility" />
              <Metric label="Max drawdown" value={fpct(metrics.max_drawdown)} hint="worst peak-to-trough decline" />
              <Metric label="Volatility" value={fpct(metrics.volatility)} hint="annualized standard deviation" />
              <Metric label="Total return" value={fpct(metrics.total_return)} hint="since inception" />
              <Metric label="Annualized" value={fpct(metrics.annualized_return)} hint="compounded to a yearly rate" />
              <Metric label="Hit rate" value={metrics.hit_rate == null ? "—" : `${(metrics.hit_rate * 100).toFixed(0)}%`} hint="share of up days" />
              <Metric label="Information ratio" value={ratio(metrics.information_ratio)} hint={`excess return vs ${meta.benchmark_symbol ?? "benchmark"}, risk-adjusted`} />
            </div>
          ) : (
            <p className="text-sm text-zinc-500">
              Not enough marks to compute metrics yet — Sharpe and Sortino need at least two
              observations. Come back after the valuation job has run a few days.
            </p>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: ReactNode; hint: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-widest text-zinc-500" title={hint}>
        {label}
      </div>
      <div className="mt-1 text-xl font-semibold tnum text-zinc-100">{value}</div>
    </div>
  );
}
