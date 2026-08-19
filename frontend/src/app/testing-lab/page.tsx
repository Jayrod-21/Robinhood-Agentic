"use client";

// Testing Lab: train and compare ML models with honest, no-lookahead validation, parameter sweeps,
// crisis-scenario stress tests, and a model leaderboard. Read-only monitor over the backend's ML
// runs (the backend ports the framework-agnostic lib from the Special-Sprinkle-Sauce repo).
//
// Built mock-first (NEXT_PUBLIC_TESTLAB_MOCK=1, see lib/testingLab.ts) until the /api/testing-lab
// endpoints exist. The honesty thesis runs through the whole page: out-of-sample metrics only,
// walk-forward with a lookahead guard, information coefficient shown next to accuracy, a model that
// loses shown losing, and a loud caveat that Sharpe/profit-factor are a directional P&L proxy until
// real returns are wired.

import { useState } from "react";
import useSWR from "swr";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceDot, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { AlertTriangle, Database, ShieldCheck } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { cn, plColor } from "@/lib/format";
import {
  DATASET_LABEL, MODEL_LABEL, MOCK_TESTING_LAB, TESTLAB_MOCK, mockSweep,
  type ComparisonResponse, type Experiment, type ModelKind, type ModelMetrics,
  type StressResponse, type TestingLabResponse,
} from "@/lib/testingLab";

const AXIS = "#71717a";
const GRID = "#2e323b";
const TIP = { background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 } as const;

const acc = (n: number) => `${(n * 100).toFixed(1)}%`;
const ic = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(3)}`;
const two = (n: number) => n.toFixed(2);

type Tab = "experiments" | "comparison" | "sweeps" | "stress";
const TABS: { id: Tab; label: string }[] = [
  { id: "experiments", label: "Experiments" },
  { id: "comparison", label: "Comparison" },
  { id: "sweeps", label: "Sweeps" },
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
      subtitle={resp ? `${DATASET_LABEL[resp.meta.data_source]} data · ${resp.experiments.length} runs` : "Train and compare models, honestly"}
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
                  The Testing Lab backend isn&apos;t available yet. This page trains and compares models via
                  the /api/testing-lab endpoints; it fills in once they&apos;re live.
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
            <AlertTriangle className="h-3.5 w-3.5" /> {DATASET_LABEL[resp.meta.data_source]} data, not live
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
      {tab === "sweeps" && <SweepsTab />}
      {tab === "stress" && <StressTab stress={resp.stress} />}
    </div>
  );
}

// ── Experiments ────────────────────────────────────────────────────────────────────────────────
function ExperimentsTab({ experiments }: { experiments: Experiment[] }) {
  const rows = [...experiments].sort(
    (a, b) => (b.metrics?.information_coefficient ?? -1) - (a.metrics?.information_coefficient ?? -1),
  );
  const [selected, setSelected] = useState<string>(rows[0]?.id ?? "");
  const sel = rows.find((r) => r.id === selected) ?? rows[0];

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
                <th className="px-5 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Validation</th>
                <th className="px-3 py-2 text-right font-medium">Accuracy</th>
                <th className="px-3 py-2 text-right font-medium" title="Correlation of predicted probability with realized direction. The honest signal-quality number.">IC</th>
                <th className="px-3 py-2 text-right font-medium">Sharpe*</th>
                <th className="px-3 py-2 text-right font-medium">Max DD*</th>
                <th className="px-5 py-2 font-medium">Status</th>
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
                  <td className="px-5 py-2.5 font-medium text-zinc-100">
                    {MODEL_LABEL[r.model]}
                    {r.is_baseline && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-brass">baseline</span>}
                  </td>
                  <td className="px-3 py-2.5 text-zinc-400">{r.validation_kind.replace(/_/g, " ")}</td>
                  <td className="px-3 py-2.5 text-right tnum text-zinc-200">{r.metrics ? acc(r.metrics.accuracy) : "—"}</td>
                  <td className={cn("px-3 py-2.5 text-right tnum", r.metrics ? plColor(r.metrics.information_coefficient) : "text-zinc-600")}>
                    {r.metrics ? ic(r.metrics.information_coefficient) : "—"}
                  </td>
                  <td className={cn("px-3 py-2.5 text-right tnum", r.metrics ? plColor(r.metrics.sharpe_ratio) : "text-zinc-600")}>
                    {r.metrics ? two(r.metrics.sharpe_ratio) : "—"}
                  </td>
                  <td className="px-3 py-2.5 text-right tnum text-loss">{r.metrics ? two(r.metrics.max_drawdown) : "—"}</td>
                  <td className="px-5 py-2.5">
                    <Badge tone={r.status === "complete" ? "buy" : r.status === "failed" ? "sell" : "hold"}>{r.status}</Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      {sel && (
        <Card>
          <CardHeader className="flex items-center justify-between">
            <CardTitle>{MODEL_LABEL[sel.model]}: walk-forward accuracy by step</CardTitle>
            <span className="text-xs text-zinc-500">{sel.n_features} features · {sel.steps.length} steps</span>
          </CardHeader>
          <CardBody>
            {sel.notes && <p className="mb-3 text-xs text-flat">{sel.notes}</p>}
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={sel.steps} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="step" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={{ stroke: GRID }} tickFormatter={(v) => `#${v}`} />
                  <YAxis domain={[0.35, 0.7]} tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={false} width={44} tickFormatter={(v) => `${Math.round(v * 100)}%`} />
                  {/* Coin-flip line: skill is distance above this, sustained. */}
                  <ReferenceLine y={0.5} stroke="#71717a" strokeDasharray="4 4" label={{ value: "coin flip", fill: "#a1a1aa", fontSize: 10, position: "insideTopRight" }} />
                  <Tooltip contentStyle={TIP} labelFormatter={(l) => `step #${l}`} formatter={(v: number) => [acc(v), "accuracy"]} />
                  <Line type="monotone" dataKey="accuracy" stroke="#e0b34d" strokeWidth={1.5} dot={{ r: 2, fill: "#e0b34d" }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </CardBody>
        </Card>
      )}

      <p className="text-[11px] leading-relaxed text-zinc-600">
        * Sharpe and Max DD use the directional P&amp;L proxy noted above. Ranked by information coefficient.
        Running a new experiment posts to <span className="tnum">/api/testing-lab/experiments/run</span> (backend pending).
      </p>
    </div>
  );
}

// ── Comparison ─────────────────────────────────────────────────────────────────────────────────
const LEADERBOARD_COLS: { key: keyof ModelMetrics; label: string; fmt: (n: number) => string; color?: boolean }[] = [
  { key: "information_coefficient", label: "IC", fmt: ic, color: true },
  { key: "accuracy", label: "Accuracy", fmt: acc },
  { key: "precision", label: "Precision", fmt: acc },
  { key: "f1", label: "F1", fmt: acc },
  { key: "sharpe_ratio", label: "Sharpe*", fmt: two, color: true },
  { key: "profit_factor", label: "PF*", fmt: two },
];

function ComparisonTab({ comparison }: { comparison: ComparisonResponse }) {
  const rows = [...comparison.rows].sort((a, b) => b.metrics[comparison.ranked_by] - a.metrics[comparison.ranked_by]);
  const icData = rows.map((r) => ({ model: MODEL_LABEL[r.model], ic: r.metrics.information_coefficient, is_best: r.is_best }));
  const d = comparison.disagreement;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
        <StatCard label="Best model" value={MODEL_LABEL[rows[0].model]} sub={`by ${String(comparison.ranked_by).replace(/_/g, " ")}`} valueClass="text-gain" />
        <StatCard label="Model agreement" value={`${d.mean_pairwise_agreement_pct.toFixed(0)}%`} sub={`${d.unanimous_pct.toFixed(0)}% unanimous`} valueClass={d.high_disagreement_flag ? "text-flat" : undefined} />
        <StatCard label="Consensus" value={d.high_disagreement_flag ? "Low" : "OK"} sub={d.high_disagreement_flag ? "high disagreement, distrust the composite" : "models broadly agree"} valueClass={d.high_disagreement_flag ? "text-loss" : "text-gain"} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Leaderboard</CardTitle>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">Model</th>
                {LEADERBOARD_COLS.map((c) => (
                  <th key={c.key} className="px-3 py-2 text-right font-medium">{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.model} className={cn("border-b border-ink-850 last:border-0", r.is_best && "bg-gain/5")}>
                  <td className="px-5 py-2.5 font-medium text-zinc-100">
                    {MODEL_LABEL[r.model]}
                    {r.is_best && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-gain">best</span>}
                  </td>
                  {LEADERBOARD_COLS.map((c) => (
                    <td key={c.key} className={cn("px-3 py-2.5 text-right tnum", c.color ? plColor(r.metrics[c.key]) : "text-zinc-200")}>
                      {c.fmt(r.metrics[c.key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

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

// ── Sweeps ─────────────────────────────────────────────────────────────────────────────────────
const SWEEP_METRICS: { key: "sharpe_ratio" | "win_rate" | "max_drawdown"; label: string }[] = [
  { key: "sharpe_ratio", label: "Sharpe*" },
  { key: "win_rate", label: "Win rate" },
  { key: "max_drawdown", label: "Max DD*" },
];

function SweepsTab() {
  const sweep = mockSweep("xgboost", "max_depth");
  const [metric, setMetric] = useState<(typeof SWEEP_METRICS)[number]["key"]>("sharpe_ratio");
  const best = sweep.points.reduce((a, b) => (metric === "max_drawdown" ? (b[metric] > a[metric] ? b : a) : b[metric] > a[metric] ? b : a));
  const fmt = metric === "win_rate" ? acc : two;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>{MODEL_LABEL[sweep.model]}: {sweep.param.replace(/_/g, " ")} sweep</CardTitle>
          <div className="flex gap-1">
            {SWEEP_METRICS.map((m) => (
              <button
                key={m.key}
                onClick={() => setMetric(m.key)}
                className={cn("rounded-md px-2 py-1 text-xs transition-colors", metric === m.key ? "bg-ink-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-300")}
              >
                {m.label}
              </button>
            ))}
          </div>
        </CardHeader>
        <CardBody>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={sweep.points} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="value" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={{ stroke: GRID }} />
                <YAxis tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={false} width={48} tickFormatter={(v) => (metric === "win_rate" ? `${Math.round(v * 100)}%` : v.toFixed(1))} />
                <Tooltip contentStyle={TIP} labelFormatter={(l) => `${sweep.param} = ${l}`} formatter={(v: number) => [fmt(v), SWEEP_METRICS.find((m) => m.key === metric)!.label]} />
                <Line type="monotone" dataKey={metric} stroke="#e0b34d" strokeWidth={1.5} dot={{ r: 2, fill: "#e0b34d" }} />
                <ReferenceDot x={best.value} y={best[metric]} r={5} fill="#34d399" stroke="none" />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            Best {SWEEP_METRICS.find((m) => m.key === metric)!.label} at <span className="text-gain">{sweep.param} = {best.value}</span>.
            The inverted-U is the point: past the peak, more complexity overfits and out-of-sample skill falls.
          </p>
        </CardBody>
      </Card>
      <p className="text-[11px] text-zinc-600">Live sweeps post to <span className="tnum">/api/testing-lab/sweeps</span> (backend pending); this shows the shape.</p>
    </div>
  );
}

// ── Stress ─────────────────────────────────────────────────────────────────────────────────────
function StressTab({ stress }: { stress: StressResponse }) {
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
