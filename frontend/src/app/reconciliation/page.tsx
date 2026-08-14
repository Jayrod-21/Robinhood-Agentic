"use client";

import useSWR from "swr";
import { AlertTriangle, CheckCircle2, Database, ShieldAlert } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, cn, pct, plColor, usd } from "@/lib/format";
import { MOCK_RECONCILIATION, RECON_MOCK, type PositionStatus, type ReconciliationResponse } from "@/lib/reconciliation";

const fmtWeight = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)}%`);

// Subset of the Badge component's tone keys (ui.tsx), enough for the four reconciliation statuses.
type BadgeTone = "buy" | "sell" | "hold" | "escalated";

// Problems first: an entry the book never recorded is the loudest, then a missing exit, then drift,
// then the rows that are fine.
const STATUS_ORDER: Record<PositionStatus, number> = { unexpected: 0, missing: 1, drifted: 2, match: 3 };

const STATUS_TONE: Record<PositionStatus, BadgeTone> = {
  match: "buy",
  drifted: "hold",
  missing: "sell",
  unexpected: "escalated",
};

const STATUS_LABEL: Record<PositionStatus, string> = {
  match: "on target",
  drifted: "drifted",
  missing: "missing",
  unexpected: "unrecorded",
};

export default function ReconciliationPage() {
  const { data, error, isLoading } = useSWR<ReconciliationResponse>(
    RECON_MOCK ? null : "/api/reconciliation",
    fetcher,
    { refreshInterval: 30_000 },
  );
  const resp = RECON_MOCK ? MOCK_RECONCILIATION : data;

  const header = (
    <PageHeader
      title="Reconciliation"
      subtitle={
        resp
          ? `${resp.meta.slate_source} (${resp.meta.slate_dated}) vs broker, snapshot ${ago(resp.meta.snapshot_generated_at)}`
          : "Documented slate vs broker truth"
      }
      right={RECON_MOCK ? <Badge tone="hold">MOCK DATA</Badge> : undefined}
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
                  The account snapshot or slate isn&apos;t available, so there&apos;s nothing to
                  reconcile yet. This page diffs the documented slate against broker truth; it fills
                  in once both are readable.
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
          <Spinner /> Diffing slate against broker…
        </div>
      </div>
    );
  }

  const { meta, positions, checks, summary } = resp;
  const sorted = [...positions].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.symbol.localeCompare(b.symbol));
  const problems = summary.missing + summary.unexpected + summary.drifted;
  const depositGap = meta.account_value != null && meta.documented_book_value != null && meta.account_value - meta.documented_book_value > 1;

  return (
    <div>
      {header}

      {/* The headline verdict: is broker truth what the repo says it is? */}
      {meta.in_sync ? (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-gain/30 bg-gain/10 px-3 py-2 text-sm text-gain">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          <span>The broker holds what the slate says it holds. Nothing to reconcile.</span>
        </div>
      ) : (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-loss/40 bg-loss/10 px-3 py-2 text-sm text-loss">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            <span className="font-medium">The book has drifted from the documented slate.</span>{" "}
            {summary.missing} held nowhere, {summary.unexpected} held but never recorded,{" "}
            {summary.drifted} off target. The theses and cycle reports have been reasoning about a
            portfolio that no longer exists. Record the exits and entries, then refresh the slate.
          </span>
        </div>
      )}

      {meta.snapshot_stale && (
        <div className="mb-3 flex items-start gap-2 text-xs text-flat">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            Reconciling against a snapshot from {ago(meta.snapshot_generated_at)}. Refresh from the
            broker for current truth before acting on any breach below.
          </span>
        </div>
      )}
      {depositGap && (
        <div className="mb-3 flex items-start gap-2 text-xs text-zinc-500">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-600" />
          <span>
            Account value {usd(meta.account_value)} against a documented {usd(meta.documented_book_value)} book:
            deposits happened that the slate never accounted for.
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="On target" value={String(summary.matched)} sub="held and documented" valueClass={summary.matched ? "text-gain" : "text-zinc-400"} />
        <StatCard label="Drifted" value={String(summary.drifted)} sub="off target weight" valueClass={summary.drifted ? "text-flat" : "text-zinc-400"} />
        <StatCard label="Missing" value={String(summary.missing)} sub="documented, not held" valueClass={summary.missing ? "text-loss" : "text-zinc-400"} />
        <StatCard label="Unrecorded" value={String(summary.unexpected)} sub="held, never recorded" valueClass={summary.unexpected ? "text-fuchsia-400" : "text-zinc-400"} />
      </div>

      {/* Position-by-position diff. */}
      <Card className="mt-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Slate vs broker</CardTitle>
          <span className="text-xs text-zinc-500">
            cash {fmtWeight(meta.live_cash_pct)} live / {fmtWeight(meta.target_cash_pct)} target
          </span>
        </CardHeader>
        <CardBody className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="px-5 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 text-right font-medium">Target</th>
                <th className="px-3 py-2 text-right font-medium">Live</th>
                <th className="px-3 py-2 text-right font-medium">Drift</th>
                <th className="px-3 py-2 text-right font-medium">P&amp;L</th>
                <th className="px-3 py-2 text-center font-medium">Status</th>
                <th className="px-5 py-2 font-medium">Note</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((p) => (
                <tr key={p.symbol} className="border-b border-ink-850 last:border-0 align-top hover:bg-ink-850/50">
                  <td className="px-5 py-2.5 font-medium text-zinc-100">
                    {p.symbol}
                    {!p.in_universe && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-fuchsia-400">off-universe</span>}
                  </td>
                  <td className="px-3 py-2.5 text-right tnum text-zinc-400">{fmtWeight(p.target_weight_pct)}</td>
                  <td className="px-3 py-2.5 text-right tnum text-zinc-200">{fmtWeight(p.live_weight_pct)}</td>
                  <td className={cn("px-3 py-2.5 text-right tnum", p.drift_pct == null ? "text-zinc-600" : plColor(p.drift_pct))}>
                    {p.drift_pct == null ? "—" : `${p.drift_pct > 0 ? "+" : ""}${p.drift_pct.toFixed(1)}`}
                  </td>
                  <td className={cn("px-3 py-2.5 text-right tnum", plColor(p.unrealized_pl_pct))}>{p.unrealized_pl_pct == null ? "—" : pct(p.unrealized_pl_pct)}</td>
                  <td className="px-3 py-2.5 text-center">
                    <Badge tone={STATUS_TONE[p.status]}>{STATUS_LABEL[p.status]}</Badge>
                  </td>
                  <td className="px-5 py-2.5 text-xs text-zinc-500">{p.note ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      {/* Charter discipline checks. */}
      <Card className="mt-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Discipline checks</CardTitle>
          <span className="text-xs text-zinc-500">
            {summary.checks_failing} of {summary.checks_total} breached
          </span>
        </CardHeader>
        <CardBody className="space-y-2">
          {checks.map((c) => {
            const breach = c.status === "breach";
            const tone = !breach ? "text-gain" : c.severity === "alert" ? "text-loss" : "text-flat";
            const Icon = !breach ? CheckCircle2 : c.severity === "alert" ? ShieldAlert : AlertTriangle;
            return (
              <div key={c.rule} className="flex items-start gap-3 rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2">
                <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", tone)} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="text-sm text-zinc-200">{c.rule}</span>
                    <span className="shrink-0 text-[11px] uppercase tracking-wide text-zinc-600">{c.source}</span>
                  </div>
                  <p className={cn("mt-0.5 text-xs", breach ? tone : "text-zinc-500")}>{c.detail}</p>
                </div>
              </div>
            );
          })}
        </CardBody>
      </Card>
    </div>
  );
}
