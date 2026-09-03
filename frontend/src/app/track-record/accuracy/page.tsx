"use client";

// Learning surface (issue #145): what the system got right, what broke, and what silently didn't
// happen. Read-only over GET /api/learning. The honesty thesis is the whole point here: an unscored
// judgment is ABSENT, never counted wrong; every rate carries its denominator; and one regime is not
// a track record, so the page reads directions, not decimals. Built mock-first
// (NEXT_PUBLIC_LEARNING_MOCK=1, see lib/learning.ts) until the endpoint exists.

import Link from "next/link";
import useSWR from "swr";
import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { AlertTriangle, ArrowRight, CircleHelp, ShieldCheck } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, cn } from "@/lib/format";
import {
  LEARNING_MOCK, MOCK_LEARNING,
  type AccuracyRow, type JobStatus, type LearningResponse,
} from "@/lib/learning";

const AXIS = "#71717a";
const GRID = "#2e323b";
const TIP = { background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 } as const;

const pctText = (n: number) => `${(n * 100).toFixed(1)}%`;
const COIN_FLIP = 0.5;

/** Colour a rate by how it reads: below a coin flip is worse than useless (anti-predictive). */
function rateColor(accuracy: number | null): string {
  if (accuracy == null) return "text-zinc-600";
  if (accuracy < COIN_FLIP) return "text-loss";
  if (accuracy >= 0.6) return "text-gain";
  return "text-zinc-300";
}

function statusTone(status: JobStatus): "buy" | "sell" | "hold" | "neutral" {
  if (status === "ok") return "buy";
  if (status === "failed") return "sell";
  if (status === "stale") return "hold";
  return "neutral"; // never
}

export default function LearningPage() {
  const { data, error, isLoading } = useSWR<LearningResponse>(
    LEARNING_MOCK ? null : "/api/learning",
    fetcher,
    { refreshInterval: 60_000 },
  );
  const resp = LEARNING_MOCK ? MOCK_LEARNING : data;

  const header = (
    <PageHeader
      title="Learning"
      subtitle="What the calls got right, what broke, and what silently didn't happen."
      right={LEARNING_MOCK ? <Badge tone="hold">MOCK DATA</Badge> : undefined}
    />
  );

  if (error) {
    const degraded = /\b(404|501|503)\b/.test(String(error.message ?? error));
    return (
      <div>
        {header}
        <Card className={cn("border-loss/40", degraded && "border-ink-700")}>
          <CardBody className="flex items-start gap-3 pt-5 text-sm">
            <AlertTriangle className={cn("mt-0.5 h-4 w-4 shrink-0", degraded ? "text-zinc-400" : "text-loss")} />
            <span className={degraded ? "text-zinc-400" : "text-loss"}>
              {degraded
                ? "The learning endpoint isn't available yet. This page aggregates scored judgments and job health from /api/learning; it fills in once that's live."
                : String(error.message ?? error)}
            </span>
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
          <Spinner /> Aggregating scored judgments…
        </div>
      </div>
    );
  }

  const { meta, scoring, calibration, gaps, engine_versions } = resp;
  const overconfidentBy =
    calibration.base_rate != null && calibration.mean_confidence != null
      ? calibration.mean_confidence - calibration.base_rate
      : null;

  return (
    <div className="space-y-6">
      {header}

      {/* The standing caveat: one regime is not a track record, and a gap is not a wrong answer. */}
      <div className="flex items-start gap-2 rounded-lg border border-flat/30 bg-flat/5 px-3 py-2 text-xs text-zinc-400">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-flat" />
        <span>
          {meta.regime_note || "One regime is not a track record."}{" "}
          An unscored judgment is <span className="text-zinc-200">absent</span>, never counted wrong, and
          every rate below is shown with its sample size.
        </span>
      </div>

      {/* ── 1. Did the calls turn out right? ─────────────────────────────────────────────────── */}
      <section className="space-y-4">
        <SectionTitle>Did the calls turn out right?</SectionTitle>

        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard
            label="Scored"
            value={`${scoring.scored.toLocaleString()}`}
            sub={`of ${scoring.total_judgments.toLocaleString()} judgments`}
          />
          <StatCard
            label="Never scored"
            value={scoring.unscored_absent.toLocaleString()}
            sub="window elapsed, absent not wrong"
            valueClass={scoring.unscored_absent > 0 ? "text-flat" : "text-gain"}
          />
          <StatCard
            label="Base rate"
            value={calibration.base_rate != null ? pctText(calibration.base_rate) : "—"}
            sub={`realized, n=${calibration.n.toLocaleString()}`}
          />
          <StatCard
            label="Confidence gap"
            value={overconfidentBy != null ? `${overconfidentBy >= 0 ? "+" : ""}${(overconfidentBy * 100).toFixed(1)}pp` : "—"}
            sub={overconfidentBy != null && overconfidentBy > 0 ? "overconfident" : "stated vs realized"}
            valueClass={overconfidentBy != null && overconfidentBy > 0.05 ? "text-flat" : undefined}
          />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Accuracy by decision</CardTitle>
            <span className="text-xs text-zinc-500">a rate below a coin flip is anti-predictive</span>
          </CardHeader>
          <CardBody className="p-0">
            <AccuracyTable rows={scoring.by_decision} minN={scoring.min_n} keyLabel="Decision" flagAntiPredictive />
          </CardBody>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Accuracy by lens</CardTitle>
              <span className="text-xs text-zinc-500">is a lens better, or does it just vote?</span>
            </CardHeader>
            <CardBody className="p-0">
              <AccuracyTable rows={scoring.by_lens} minN={scoring.min_n} keyLabel="Lens" />
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Accuracy by model family</CardTitle>
              <span className="text-xs text-zinc-500">measurable since #142</span>
            </CardHeader>
            <CardBody className="p-0">
              <AccuracyTable rows={scoring.by_family} minN={scoring.min_n} keyLabel="Family" family />
            </CardBody>
          </Card>
        </div>

        <Card>
          <CardHeader className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Accuracy over time</CardTitle>
            <Link href="/track-record/calibration" className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-200">
              full reliability curve <ArrowRight className="h-3 w-3" />
            </Link>
          </CardHeader>
          <CardBody>
            <div className="h-52">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={scoring.trend} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="period" tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={{ stroke: GRID }} />
                  <YAxis domain={[0.3, 0.8]} tick={{ fill: AXIS, fontSize: 11 }} tickLine={false} axisLine={false} width={44} tickFormatter={(v) => `${Math.round(v * 100)}%`} />
                  <ReferenceLine y={COIN_FLIP} stroke="#71717a" strokeDasharray="4 4" label={{ value: "coin flip", fill: "#a1a1aa", fontSize: 10, position: "insideTopRight" }} />
                  <Tooltip
                    contentStyle={TIP}
                    formatter={(v: number, _n, p) => [`${pctText(v)} (n=${p.payload.n})`, "accuracy"]}
                  />
                  <Line type="monotone" dataKey="accuracy" stroke="#e0b34d" strokeWidth={1.5} dot={{ r: 3, fill: "#e0b34d" }} connectNulls={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p className="mt-2 text-[11px] text-zinc-600">Each point carries its own n; a jump on a small sample is noise, not improvement.</p>
          </CardBody>
        </Card>
      </section>

      {/* ── 2. What did NOT happen? ──────────────────────────────────────────────────────────── */}
      <section className="space-y-4">
        <SectionTitle>What didn&apos;t happen that should have?</SectionTitle>

        <Card>
          <CardHeader>
            <CardTitle>Job health</CardTitle>
            <span className="text-xs text-zinc-500">a job that never ran is distinct from one that ran and failed</span>
          </CardHeader>
          <CardBody className="overflow-x-auto p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                  <th className="px-5 py-2 font-medium">Job</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Schedule</th>
                  <th className="px-3 py-2 font-medium">Last run</th>
                  <th className="px-5 py-2 font-medium">Detail</th>
                </tr>
              </thead>
              <tbody>
                {gaps.jobs.map((j) => (
                  <tr key={j.name} className="border-b border-ink-850 last:border-0">
                    <td className="px-5 py-2.5 font-medium tnum text-zinc-100">{j.name}</td>
                    <td className="px-3 py-2.5"><Badge tone={statusTone(j.status)}>{j.status}</Badge></td>
                    <td className="px-3 py-2.5 text-zinc-500">{j.schedule ?? "—"}</td>
                    <td className="px-3 py-2.5 text-zinc-400">{j.last_run ? ago(j.last_run) : "never"}</td>
                    <td className="px-5 py-2.5 text-xs text-zinc-500">{j.detail ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardBody>
        </Card>

        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
          <StatCard
            label="Debates with abstentions"
            value={String(gaps.debates_with_abstentions)}
            sub={`${gaps.total_abstentions} juror abstentions total`}
            valueClass={gaps.total_abstentions > 0 ? "text-flat" : "text-gain"}
          />
          <StatCard
            label="Unreconciled cycles"
            value={String(gaps.unreconciled_cycles)}
            sub="never reconciled (not: found no drift)"
            valueClass={gaps.unreconciled_cycles > 0 ? "text-flat" : "text-gain"}
          />
          <StatCard
            label="Unscored judgments"
            value={gaps.unscored_judgments.toLocaleString()}
            sub="window elapsed, never scored"
            valueClass={gaps.unscored_judgments > 0 ? "text-flat" : "text-gain"}
          />
        </div>
      </section>

      {/* ── 3. What changed, and did it help? ────────────────────────────────────────────────── */}
      <section className="space-y-4">
        <SectionTitle>What changed, and did it help?</SectionTitle>
        <Card>
          <CardHeader>
            <CardTitle>Accuracy by engine version</CardTitle>
            <span className="text-xs text-zinc-500">a verdict tied to the version that produced it</span>
          </CardHeader>
          <CardBody className="overflow-x-auto p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                  <th className="px-5 py-2 font-medium">Version</th>
                  <th className="px-3 py-2 font-medium">Window</th>
                  <th className="px-3 py-2 text-right font-medium">n</th>
                  <th className="px-3 py-2 text-right font-medium">Accuracy</th>
                  <th className="px-5 py-2 font-medium">What changed</th>
                </tr>
              </thead>
              <tbody>
                {engine_versions.map((v) => (
                  <tr key={v.version} className="border-b border-ink-850 last:border-0">
                    <td className="px-5 py-2.5 font-medium tnum text-zinc-100">
                      {v.version}
                      {v.to == null && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-brass">current</span>}
                    </td>
                    <td className="px-3 py-2.5 tnum text-zinc-500">{fmtDate(v.from)} – {v.to ? fmtDate(v.to) : "now"}</td>
                    <td className="px-3 py-2.5 text-right tnum text-zinc-400">{v.n.toLocaleString()}</td>
                    <td className={cn("px-3 py-2.5 text-right tnum", rateColor(v.accuracy))}>
                      {v.accuracy != null ? pctText(v.accuracy) : belowGate(v.n)}
                    </td>
                    <td className="px-5 py-2.5 text-xs text-zinc-500">{v.note ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardBody>
        </Card>
        <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-zinc-600">
          <CircleHelp className="mt-0.5 h-3 w-3 shrink-0" />
          Comparing versions across a single mixed regime cannot separate a better engine from an easier
          tape. Treat a rising line as a hypothesis to keep measuring, not a proven gain.
        </p>
      </section>

      <p className="pt-2 text-[11px] text-zinc-600">
        Last aggregated {ago(meta.generated_at)}
        {meta.window_days != null && ` · ${meta.window_days} days of scored history`}
        {meta.current_engine_version && ` · engine ${meta.current_engine_version}`}.
      </p>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="flex items-center gap-2 text-sm font-medium text-zinc-300"><ShieldCheck className="h-4 w-4 text-zinc-600" /> {children}</h2>;
}

function belowGate(n: number): string {
  return `— n=${n}`;
}

/** A rate table that never renders a gap as a zero: a bucket below min_n shows "— n=X (below Y)"
 *  rather than an accuracy, and an anti-predictive row (below a coin flip) is flagged when asked. */
function AccuracyTable({
  rows, minN, keyLabel, flagAntiPredictive = false, family = false,
}: {
  rows: AccuracyRow[];
  minN: number;
  keyLabel: string;
  flagAntiPredictive?: boolean;
  family?: boolean;
}) {
  const label = (k: string) =>
    family ? (k === "anthropic" ? "Claude" : k === "google" ? "Gemini" : k) : k.replace(/_/g, " ");
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
          <th className="px-5 py-2 font-medium">{keyLabel}</th>
          <th className="px-3 py-2 text-right font-medium">n</th>
          <th className="px-3 py-2 text-right font-medium">Correct</th>
          <th className="px-5 py-2 text-right font-medium">Accuracy</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const anti = flagAntiPredictive && r.accuracy != null && r.n >= minN && r.accuracy < COIN_FLIP;
          return (
            <tr key={r.key} className={cn("border-b border-ink-850 last:border-0", anti && "bg-loss/5")}>
              <td className="px-5 py-2.5 font-medium capitalize text-zinc-100">
                {label(r.key)}
                {anti && <span className="ml-2 text-[10px] uppercase tracking-wide text-loss">anti-predictive</span>}
              </td>
              <td className="px-3 py-2.5 text-right tnum text-zinc-400">{r.n.toLocaleString()}</td>
              <td className="px-3 py-2.5 text-right tnum text-zinc-500">{r.accuracy != null ? r.correct.toLocaleString() : "—"}</td>
              <td className={cn("px-5 py-2.5 text-right tnum", rateColor(r.accuracy))}>
                {r.accuracy != null
                  ? pctText(r.accuracy)
                  : <span className="text-zinc-600" title={`n=${r.n} is below the ${minN} needed for a meaningful rate`}>— n={r.n} &lt; {minN}</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
}
