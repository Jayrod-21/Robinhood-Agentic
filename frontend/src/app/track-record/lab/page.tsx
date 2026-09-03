"use client";

// Testing Lab: monitor the backend's ML runs with honest, no-lookahead validation and a model
// leaderboard. Read-only over GET /api/testing-lab (backend/app/routers/testing_lab.py::lab_overview),
// which aggregates the separate Lab container. The honesty thesis runs through the page: out-of-sample
// metrics only, information coefficient shown next to accuracy, a loud caveat that Sharpe/profit-factor
// are a directional P&L proxy, unmeasured runs counted rather than hidden, and a MEASURED-but-worthless
// run (a constant predictor) flagged as degenerate right on its leaderboard row.
//
// The overview's `experiments` are experiment-LEVEL rows (status/timing, no per-model metrics); the
// measured per-model numbers live on the leaderboard (Comparison tab). Per-experiment model runs and
// parameter sweeps are returned by GET /api/testing-lab/experiments/{id}, not wired into this page yet.

import { useState } from "react";
import useSWR from "swr";
import {
  Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { AlertTriangle, Database, ShieldCheck } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { cn, plColor } from "@/lib/format";
import {
  dataSourceLabel, modelLabel, MOCK_TESTING_LAB, TESTLAB_MOCK,
  type ComparisonResponse, type ExperimentRow, type ModelMetrics, type StressResponse,
  type TestingLabResponse,
} from "@/lib/testingLab";

const AXIS = "#71717a";
const GRID = "#2e323b";
const TIP = { background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 } as const;

const acc = (n: number) => `${(n * 100).toFixed(1)}%`;
const ic = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(3)}`;
const two = (n: number) => n.toFixed(2);

/** Metric access by a string key from the backend (comparison.metric, leaderboard columns). Falls back
 *  to 0 rather than NaN so a missing/renamed key never blows up the sort or a cell. */
function metricVal(m: ModelMetrics, key: string): number {
  const v = (m as unknown as Record<string, number>)[key];
  return typeof v === "number" ? v : 0;
}

function statusTone(status: string): "buy" | "sell" | "hold" | "neutral" {
  if (status === "complete") return "buy";
  if (status === "failed") return "sell";
  if (status === "running" || status === "queued") return "hold";
  return "neutral";
}

type Tab = "experiments" | "comparison" | "stress";
const TABS: { id: Tab; label: string }[] = [
  { id: "experiments", label: "Experiments" },
  { id: "comparison", label: "Comparison" },
  { id: "stress", label: "Stress" },
];

export default function TestingLabPage() {
  const { data, error, isLoading } = useSWR<TestingLabResponse>(
    TESTLAB_MOCK ? null : "/api/testing-lab",
    fetcher,
    { refreshInterval: 30_000 },
  );
  const resp = TESTLAB_MOCK ? MOCK_TESTING_LAB : data;
  const [tab, setTab] = useState<Tab>("experiments");

  const header = (
    <PageHeader
      title="Testing Lab"
      subtitle={resp ? `${dataSourceLabel(resp.meta.data_source)} data · ${resp.experiments.length} runs` : "Train and compare models, honestly"}
      right={TESTLAB_MOCK ? <Badge tone="hold">MOCK DATA</Badge> : undefined}
    />
  );

  if (error) {
    const degraded = /\b(404|501|503)\b/.test(String(error.message ?? error));
    return (
      <div>
        {header}
        <Card className={cn("border-loss/40", degraded && "border-ink-700")}>
          <CardBody className="flex items-start gap-3 pt-5 text-sm">
            {degraded ? (
              <>
                <Database className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" />
                <span className="text-zinc-400">
                  The Testing Lab backend isn&apos;t available yet. This page monitors the /api/testing-lab
                  model runs; it fills in once the Lab is reachable.
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
          <Spinner /> Loading model runs…
        </div>
      </div>
    );
  }

  return (
    <div>
      {header}

      {/* Honesty strip: what these numbers are and are not. */}
      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        {resp.meta.out_of_sample && (
          <span className="inline-flex items-center gap-1.5 text-gain">
            <ShieldCheck className="h-3.5 w-3.5" /> out-of-sample only
          </span>
        )}
        <span className="inline-flex items-center gap-1.5 text-gain">
          <ShieldCheck className="h-3.5 w-3.5" /> walk-forward, lookahead-guarded
        </span>
        {resp.meta.data_source !== "live_bars" && (
          <span className="inline-flex items-center gap-1.5 text-flat">
            <AlertTriangle className="h-3.5 w-3.5" /> {dataSourceLabel(resp.meta.data_source)} data, not live
          </span>
        )}
      </div>

      {resp.meta.proxy_pnl && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-flat/30 bg-flat/5 px-3 py-2 text-xs text-zinc-400">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-flat" />
          <span>
            Sharpe and profit factor are computed on a simplified +1/-1 per-call P&amp;L proxy, not real
            returns, until live bars are wired. Read <span className="text-zinc-200">accuracy</span> and{" "}
            <span className="text-zinc-200">information coefficient</span> as the honest signal for now.
          </span>
        </div>
      )}

      {/* Tabs */}
      <div className="mb-4 flex gap-1 border-b border-ink-800">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm transition-colors",
              tab === t.id ? "border-brass text-zinc-100" : "border-transparent text-zinc-500 hover:text-zinc-300",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "experiments" && <ExperimentsTab experiments={resp.experiments} />}
      {tab === "comparison" && <ComparisonTab comparison={resp.comparison} />}
      {tab === "stress" && <StressTab stress={resp.stress} />}
    </div>
  );
}

// ── Experiments ────────────────────────────────────────────────────────────────────────────────
function ExperimentsTab({ experiments }: { experiments: ExperimentRow[] }) {
  const rows = [...experiments].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  const [selected, setSelected] = useState<number>(rows[0]?.id ?? -1);
  const sel = rows.find((r) => r.id === selected) ?? rows[0];

  if (rows.length === 0) {
    return (
      <Card>
        <CardBody className="pt-5 text-sm text-zinc-400">
          No experiments have been run yet. A run posts to{" "}
          <span className="tnum text-zinc-300">/api/testing-lab/experiments/run</span> and shows up here.
        </CardBody>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Runs</CardTitle>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">Experiment</th>
                <th className="px-3 py-2 font-medium">Data</th>
                <th className="px-3 py-2 font-medium">Validation</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-5 py-2 font-medium">Started</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r.id)}
                  className={cn(
                    "cursor-pointer border-b border-ink-850 last:border-0 hover:bg-ink-850/50",
                    r.id === sel?.id && "bg-ink-850/60",
                  )}
                >
                  <td className="px-5 py-2.5">
                    <span className="font-medium text-zinc-100">{r.name}</span>
                    {r.kind && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-zinc-500">{r.kind}</span>}
                  </td>
                  <td className="px-3 py-2.5 text-zinc-400">{dataSourceLabel(r.data_source)}</td>
                  <td className="px-3 py-2.5 text-zinc-400">{r.validation_kind.replace(/_/g, " ")}</td>
                  <td className="px-3 py-2.5">
                    <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                  </td>
                  <td className="px-5 py-2.5 tnum text-zinc-500">{fmtTime(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      {sel && (
        <Card>
          <CardHeader className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>{sel.name}</CardTitle>
            <span className="text-xs text-zinc-500">
              run #{sel.id}{sel.operator ? ` · ${sel.operator}` : ""}
            </span>
          </CardHeader>
          <CardBody className="space-y-3 text-sm">
            {sel.error && (
              <div className="flex items-start gap-2 rounded-md border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{sel.error}</span>
              </div>
            )}
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
              <Field label="Kind" value={sel.kind || "—"} />
              <Field label="Dataset" value={dataSourceLabel(sel.dataset)} />
              <Field label="Validation" value={sel.validation_kind.replace(/_/g, " ")} />
              <Field label="Started" value={fmtTime(sel.created_at)} />
              <Field label="Finished" value={sel.completed_at ? fmtTime(sel.completed_at) : "—"} />
              <Field label="Operator" value={sel.operator ?? "—"} />
            </dl>
            {sel.params && Object.keys(sel.params).length > 0 && (
              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wider text-zinc-600">Parameters</div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(sel.params).map(([k, v]) => (
                    <span key={k} className="rounded bg-ink-850 px-2 py-0.5 text-xs text-zinc-400">
                      <span className="text-zinc-600">{k}</span> {String(v)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </CardBody>
        </Card>
      )}

      <p className="text-[11px] leading-relaxed text-zinc-600">
        Per-model metrics are on the Comparison tab. A run&apos;s individual model results, walk-forward
        steps, and parameter sweeps come from{" "}
        <span className="tnum">/api/testing-lab/experiments/{"{id}"}</span> (not wired into this page yet).
      </p>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-zinc-600">{label}</dt>
      <dd className="text-zinc-300">{value}</dd>
    </div>
  );
}

// ── Comparison ─────────────────────────────────────────────────────────────────────────────────
const LEADERBOARD_COLS: { key: string; label: string; fmt: (n: number) => string; color?: boolean }[] = [
  { key: "information_coefficient", label: "IC", fmt: ic, color: true },
  { key: "accuracy", label: "Accuracy", fmt: acc },
  { key: "precision", label: "Precision", fmt: acc },
  { key: "f1", label: "F1", fmt: acc },
  { key: "sharpe_ratio", label: "Sharpe*", fmt: two, color: true },
  { key: "profit_factor", label: "PF*", fmt: two },
];

function ComparisonTab({ comparison }: { comparison: ComparisonResponse }) {
  const rankKey = comparison.metric || "information_coefficient";
  const rows = [...comparison.models].sort((a, b) => metricVal(b.metrics, rankKey) - metricVal(a.metrics, rankKey));
  const flagged = rows.filter((r) => r.degenerate.length > 0);

  if (rows.length === 0) {
    return (
      <Card>
        <CardBody className="pt-5 text-sm text-zinc-400">
          No measured runs to rank yet.
          {comparison.unmeasured_runs > 0 && (
            <> {comparison.unmeasured_runs} run{comparison.unmeasured_runs === 1 ? "" : "s"} scored nothing and {comparison.unmeasured_runs === 1 ? "was" : "were"} excluded.</>
          )}
        </CardBody>
      </Card>
    );
  }

  const best = rows[0];
  const icData = rows.map((r) => ({ model: modelLabel(r.model), ic: r.metrics.information_coefficient }));

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
        <StatCard label="Best model" value={modelLabel(best.model)} sub={`by ${rankKey.replace(/_/g, " ")}`} valueClass="text-gain" />
        <StatCard label="Measured runs" value={String(rows.length)} sub="ranked below" />
        <StatCard
          label="Unmeasured runs"
          value={String(comparison.unmeasured_runs)}
          sub={comparison.unmeasured_runs > 0 ? "scored nothing, excluded" : "none"}
          valueClass={comparison.unmeasured_runs > 0 ? "text-flat" : "text-gain"}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Leaderboard</CardTitle>
          <span className="text-xs text-zinc-500">best measured run per model</span>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">Model</th>
                {LEADERBOARD_COLS.map((c) => (
                  <th key={c.key} className="px-3 py-2 text-right font-medium">{c.label}</th>
                ))}
                <th className="px-3 py-2 text-right font-medium" title="Walk-forward predictions scored / failed">Preds</th>
                <th className="px-5 py-2 font-medium">Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.run_id} className={cn("border-b border-ink-850 last:border-0", i === 0 && "bg-gain/5")}>
                  <td className="px-5 py-2.5 font-medium text-zinc-100">
                    <span className="inline-flex items-center gap-1.5">
                      {r.degenerate.length > 0 && (
                        <span title="degenerate: a measured result that is still worthless (see below)">
                          <AlertTriangle className="h-3.5 w-3.5 text-flat" />
                        </span>
                      )}
                      {modelLabel(r.model)}
                    </span>
                    {i === 0 && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-gain">best</span>}
                    {r.is_baseline && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-brass">baseline</span>}
                  </td>
                  {LEADERBOARD_COLS.map((c) => (
                    <td key={c.key} className={cn("px-3 py-2.5 text-right tnum", c.color ? plColor(metricVal(r.metrics, c.key)) : "text-zinc-200")}>
                      {c.fmt(metricVal(r.metrics, c.key))}
                    </td>
                  ))}
                  <td className="px-3 py-2.5 text-right tnum text-zinc-400">
                    {r.predictions_made}
                    {r.predictions_failed > 0 && <span className="ml-1 text-loss">/ {r.predictions_failed} failed</span>}
                  </td>
                  <td className="px-5 py-2.5 text-zinc-500">{dataSourceLabel(r.data_source)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      {flagged.length > 0 && (
        <Card className="border-flat/40 bg-flat/5">
          <CardBody className="pt-5">
            <div className="flex items-center gap-2 font-medium text-flat">
              <AlertTriangle className="h-4 w-4" /> Degenerate results: measured, but not a model
            </div>
            <ul className="mt-2 space-y-1.5 text-sm text-zinc-400">
              {flagged.map((r) => (
                <li key={r.run_id}>
                  <span className="text-zinc-300">{modelLabel(r.model)}:</span> {r.degenerate.join("; ")}
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Information coefficient by model</CardTitle>
        </CardHeader>
        <CardBody>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={icData} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="model" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={{ stroke: GRID }} />
                <YAxis tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={false} width={48} tickFormatter={(v) => v.toFixed(2)} />
                <ReferenceLine y={0} stroke="#3f3f46" />
                <Tooltip contentStyle={TIP} formatter={(v: number) => [ic(v), "IC"]} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
                <Bar dataKey="ic" radius={[3, 3, 0, 0]}>
                  {icData.map((row, i) => (
                    <Cell key={i} fill={row.ic >= 0 ? "#34d399" : "#fb7185"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-2 text-[11px] text-zinc-600">IC below zero means the model&apos;s probabilities point the wrong way on this set. Shown, not hidden.</p>
        </CardBody>
      </Card>
    </div>
  );
}

// ── Stress ─────────────────────────────────────────────────────────────────────────────────────
function StressTab({ stress }: { stress: StressResponse }) {
  if (!stress.available || stress.scenarios.length === 0) {
    return (
      <Card>
        <CardBody className="flex items-start gap-3 pt-5 text-sm text-zinc-400">
          <Database className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" />
          <span>
            {stress.note || "Stress scenarios aren't available yet."} The crisis-scenario module
            (COVID 2020, 2022 bear, 2008, and the rest) is still on the port list; this tab fills in
            once it lands, rather than showing fabricated scenarios.
          </span>
        </CardBody>
      </Card>
    );
  }

  const rows = [...stress.scenarios].sort((a, b) => a.estimated_pl_pct - b.estimated_pl_pct);
  const chart = rows.map((s) => ({ label: s.label, spy: s.spy_move_pct, est: s.estimated_pl_pct }));

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Crisis scenarios</CardTitle>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">Scenario</th>
                <th className="px-3 py-2 font-medium">Period</th>
                <th className="px-3 py-2 text-right font-medium">SPY</th>
                <th className="px-3 py-2 text-right font-medium">Est. portfolio</th>
                <th className="px-5 py-2 font-medium">Worst sector</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.key} className="border-b border-ink-850 last:border-0 hover:bg-ink-850/50">
                  <td className="px-5 py-2.5 font-medium text-zinc-100">{s.label}</td>
                  <td className="px-3 py-2.5 text-zinc-500">{s.period}</td>
                  <td className="px-3 py-2.5 text-right tnum text-loss">{s.spy_move_pct.toFixed(1)}%</td>
                  <td className={cn("px-3 py-2.5 text-right tnum", plColor(s.estimated_pl_pct))}>{s.estimated_pl_pct.toFixed(1)}%</td>
                  <td className="px-5 py-2.5 text-zinc-400">{s.worst_sector}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>SPY move vs estimated portfolio impact</CardTitle>
        </CardHeader>
        <CardBody>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chart} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barGap={2}>
                <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="label" tick={{ fill: AXIS, fontSize: 10 }} tickLine={false} axisLine={{ stroke: GRID }} interval={0} angle={-12} textAnchor="end" height={48} />
                <YAxis tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={false} width={40} tickFormatter={(v) => `${v}%`} />
                <ReferenceLine y={0} stroke="#3f3f46" />
                <Tooltip contentStyle={TIP} formatter={(v: number, n) => [`${v}%`, n === "spy" ? "SPY" : "Est. portfolio"]} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
                <Bar dataKey="spy" fill="#52525b" radius={[3, 3, 0, 0]} />
                <Bar dataKey="est" fill="#fb7185" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-zinc-600">
            Estimates from historical per-sector impact multipliers applied to the current allocation, not a backtest of
            the actual strategy. A floor, not a forecast.
          </p>
        </CardBody>
      </Card>
    </div>
  );
}

function fmtTime(iso: string): string {
  // Fixed locale + UTC so server and client render identical text (no hydration mismatch).
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}
