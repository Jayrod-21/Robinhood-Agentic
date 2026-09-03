"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { AlertTriangle, Database, Target } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard, decisionTone } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { cn, pct, plColor } from "@/lib/format";
import { CALIB_MOCK, mockCalibration, type CalibrationResponse, type CalibrationScope } from "@/lib/calibration";

const DOT_COLOR = "#e0b34d";

// Fractional (0.55) → "55%". Bare, no sign (these are rates, not P&L).
const rate = (v: number | null | undefined, dp = 0) => (v == null ? "—" : `${(v * 100).toFixed(dp)}%`);
// A bare score (ECE / Brier): two decimals, em-dash when withheld below the gate.
const score = (v: number | null | undefined, dp = 2) => (v == null ? "—" : v.toFixed(dp));

function CalTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: { x: number; y: number; n: number } }> }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-lg border border-ink-700 bg-ink-850 px-3 py-2 text-xs text-zinc-300">
      <div>Predicted <span className="tnum text-zinc-100">{p.x.toFixed(0)}%</span></div>
      <div>Actual <span className="tnum text-zinc-100">{p.y.toFixed(0)}%</span></div>
      <div className="text-zinc-500">n = {p.n}</div>
    </div>
  );
}

export default function CalibrationPage() {
  const [scope, setScope] = useState<CalibrationScope>("jury");

  // Mock mode short-circuits the fetch (SWR key = null) so a missing endpoint can't read as an
  // empty record. Scope is part of the key so the toggle refetches the other confidence source.
  const { data, error, isLoading } = useSWR<CalibrationResponse>(
    CALIB_MOCK ? null : `/api/calibration?scope=${scope}`,
    fetcher,
    { refreshInterval: 60_000 },
  );
  const resp = CALIB_MOCK ? mockCalibration(scope) : data;

  const scopeToggle = (
    <div className="flex gap-1">
      <Button variant={scope === "jury" ? "brass" : "ghost"} className="px-2.5 py-1 text-xs" onClick={() => setScope("jury")}>
        Jury
      </Button>
      <Button variant={scope === "personas" ? "brass" : "ghost"} className="px-2.5 py-1 text-xs" onClick={() => setScope("personas")}>
        Personas
      </Button>
    </div>
  );

  const header = (
    <PageHeader
      title="Calibration"
      subtitle={
        resp
          ? `${resp.meta.scope === "jury" ? "jury rulings" : "persona proposals"} · ${resp.meta.outcome_definition}`
          : "Was the confidence earned?"
      }
      right={
        <div className="flex items-center gap-3">
          {CALIB_MOCK && <Badge tone="hold">MOCK DATA</Badge>}
          {resp && scopeToggle}
        </div>
      }
    />
  );

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
                  The evaluation database isn&apos;t connected, so there&apos;s no decision history to
                  score yet. This page pairs each ruling&apos;s stated confidence with its realized
                  outcome; it fills in once the DB is up.
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
          <Spinner /> Scoring the track record…
        </div>
      </div>
    );
  }

  const { meta, overall, by_agent, decisions } = resp;
  const gap = overall.mean_confidence != null && overall.base_rate != null ? overall.mean_confidence - overall.base_rate : null;
  const points = overall.bins
    .filter((b) => b.n > 0 && b.predicted != null && b.hit_rate != null)
    .map((b) => ({ x: (b.predicted as number) * 100, y: (b.hit_rate as number) * 100, n: b.n }));

  return (
    <div>
      {header}

      {/* Honesty banners. */}
      {!overall.is_calibratable && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-flat/30 bg-flat/10 px-3 py-2 text-xs text-flat">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            <span className="font-medium">Not yet calibratable.</span> {overall.n_decisions} of the{" "}
            {overall.min_n_for_calibration} scored decisions needed to read a reliability curve. The
            points below are shown for context, but there aren&apos;t enough yet to call the book
            over- or under-confident.
          </span>
        </div>
      )}
      <div className="mb-3 flex items-start gap-2 text-xs text-zinc-500">
        <Target className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-600" />
        <span>
          A decision counts as <span className="text-zinc-300">correct</span> when its{" "}
          {meta.outcome_definition}
          {meta.returns_basis === "price_only" ? " (price-only — dividends not loaded)" : ""}. Perfect
          calibration is the diagonal: a call made at 70% confidence comes true 70% of the time.
        </span>
      </div>
      {meta.coverage != null && meta.coverage < 1 && (
        <div className="mb-3 flex items-start gap-2 text-xs text-zinc-500">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-600" />
          <span>
            Coverage {(meta.coverage * 100).toFixed(1)}% of expected trading days
            {meta.coverage_note ? ` — ${meta.coverage_note}` : ""}.
          </span>
        </div>
      )}

      {/* Headline stats. */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Calibration error" value={score(overall.ece)} sub="ECE · 0 = perfect" />
        <StatCard label="Mean confidence" value={rate(overall.mean_confidence)} sub={`what the ${meta.scope === "jury" ? "jury" : "personas"} claimed`} />
        <StatCard
          label="Hit rate"
          value={rate(overall.base_rate)}
          sub={gap == null ? "realized" : `${gap > 0 ? "+" : ""}${(gap * 100).toFixed(0)} pts ${gap > 0 ? "overconfident" : "underconfident"}`}
          valueClass={gap == null ? undefined : plColor(-gap)}
        />
        <StatCard label="Decisions scored" value={String(overall.n_decisions)} sub={overall.is_calibratable ? "enough to read" : "too few to rank"} valueClass={overall.is_calibratable ? undefined : "text-zinc-400"} />
      </div>

      {/* Reliability diagram. */}
      <Card className="mt-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Reliability — predicted vs actual</CardTitle>
          <span className="text-xs text-zinc-500">dot size = decisions in bucket</span>
        </CardHeader>
        <CardBody>
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 8, right: 12, bottom: 16, left: 4 }}>
                <CartesianGrid stroke="#23262d" />
                <XAxis
                  type="number"
                  dataKey="x"
                  domain={[0, 100]}
                  ticks={[0, 25, 50, 75, 100]}
                  tick={{ fill: "#71717a", fontSize: 11 }}
                  tickLine={false}
                  axisLine={{ stroke: "#2e323b" }}
                  tickFormatter={(v: number) => `${v}%`}
                  label={{ value: "Predicted confidence", position: "insideBottom", offset: -8, fill: "#71717a", fontSize: 11 }}
                />
                <YAxis
                  type="number"
                  dataKey="y"
                  domain={[0, 100]}
                  ticks={[0, 25, 50, 75, 100]}
                  tick={{ fill: "#71717a", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  width={44}
                  tickFormatter={(v: number) => `${v}%`}
                  label={{ value: "Actual", angle: -90, position: "insideLeft", offset: 16, fill: "#71717a", fontSize: 11 }}
                />
                <ZAxis type="number" dataKey="n" range={[50, 420]} />
                {/* The diagonal = perfect calibration. */}
                <ReferenceLine segment={[{ x: 0, y: 0 }, { x: 100, y: 100 }]} stroke="#3f3f46" strokeDasharray="5 4" ifOverflow="hidden" />
                {/* Custom tooltip so scatter shows predicted/actual/n, not raw dataKeys. */}
                <Tooltip cursor={{ strokeDasharray: "3 3", stroke: "#2e323b" }} content={<CalTooltip />} />
                <Scatter data={points} fill={DOT_COLOR} fillOpacity={0.75} stroke={DOT_COLOR} />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            Dots below the diagonal = overconfident (claimed more than it delivered); above = underconfident.
          </div>
        </CardBody>
      </Card>

      {/* Per-agent record (§3.2 judges / §3.3 personas). */}
      <Card className="mt-4">
        <CardHeader>
          <CardTitle>By {meta.scope === "jury" ? "juror" : "persona"}</CardTitle>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">{meta.scope === "jury" ? "Juror" : "Persona"}</th>
                <th className="px-3 py-2 text-right font-medium">n</th>
                <th className="px-3 py-2 text-right font-medium" title="Expected calibration error — lower is better">ECE</th>
                <th className="px-3 py-2 text-right font-medium" title="Mean confidence minus realized hit rate; positive = overconfident">Over/under</th>
                <th className="px-3 py-2 text-right font-medium" title="Realized Sharpe of this agent's counterfactual paper book (§3.3)">Sharpe</th>
                <th className="px-5 py-2 text-right font-medium">Sortino</th>
              </tr>
            </thead>
            <tbody>
              {by_agent.map((a) => {
                const oc = a.mean_confidence != null && a.hit_rate != null ? a.mean_confidence - a.hit_rate : null;
                return (
                  <tr key={a.agent_id} className="border-b border-ink-850 last:border-0 hover:bg-ink-850/50">
                    <td className="px-5 py-2.5 font-medium text-zinc-100">{a.name}</td>
                    <td className="px-3 py-2.5 text-right tnum text-zinc-400">{a.n}</td>
                    <td className="px-3 py-2.5 text-right tnum text-zinc-200">{score(a.ece)}</td>
                    <td className={cn("px-3 py-2.5 text-right tnum", oc == null ? "text-zinc-500" : plColor(-oc))}>
                      {oc == null ? "—" : `${oc > 0 ? "+" : ""}${(oc * 100).toFixed(0)} pts`}
                    </td>
                    <td className="px-3 py-2.5 text-right tnum text-zinc-300">{score(a.sharpe)}</td>
                    <td className="px-5 py-2.5 text-right tnum text-zinc-300">{score(a.sortino)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </CardBody>
      </Card>

      {/* Recent scored decisions. */}
      <Card className="mt-4">
        <CardHeader>
          <CardTitle>Recent scored decisions</CardTitle>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">Date</th>
                <th className="px-3 py-2 font-medium">Ticker</th>
                <th className="px-3 py-2 font-medium">{meta.scope === "jury" ? "Juror" : "Persona"}</th>
                <th className="px-3 py-2 text-right font-medium">Confidence</th>
                <th className="px-3 py-2 text-center font-medium">Call</th>
                <th className="px-3 py-2 text-center font-medium">Outcome</th>
                <th className="px-5 py-2 text-right font-medium">Realized</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d) => (
                <tr key={`${d.debate_id}-${d.agent}`} className="border-b border-ink-850 last:border-0 hover:bg-ink-850/50">
                  <td className="px-5 py-2.5 tnum text-zinc-400">{d.created_at}</td>
                  <td className="px-3 py-2.5 font-medium text-zinc-100">{d.ticker ?? "—"}</td>
                  <td className="px-3 py-2.5 text-zinc-300">{d.agent}</td>
                  <td className="px-3 py-2.5 text-right tnum text-zinc-200">{rate(d.confidence)}</td>
                  <td className="px-3 py-2.5 text-center">
                    <Badge tone={decisionTone(d.decision.toUpperCase())}>{d.decision.toUpperCase()}</Badge>
                  </td>
                  <td className="px-3 py-2.5 text-center">
                    {d.correct == null ? (
                      <span className="text-xs text-zinc-500">pending</span>
                    ) : d.correct ? (
                      <span className="text-gain">✓</span>
                    ) : (
                      <span className="text-loss">✗</span>
                    )}
                  </td>
                  <td className={cn("px-5 py-2.5 text-right tnum", plColor(d.realized_pct))}>
                    {d.realized_pct == null ? "—" : pct(d.realized_pct * 100)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>
    </div>
  );
}
