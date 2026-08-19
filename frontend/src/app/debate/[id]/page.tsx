"use client";

// Full stage-by-stage view of one past debate (issue #26). Engine records render every stage —
// fundamentals, bull/bear cases, all 10 juror votes with full reasoning + confidence, the
// BUY/SELL/HOLD summary, decision and sizing note. Archived hand-written debates render their
// raw markdown. The reasoning is the product, not the verdict.

import Link from "next/link";
import useSWR from "swr";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner, decisionTone } from "@/components/ui";
import { FundamentalsGrid } from "@/components/fundamentals";
import { Markdown } from "@/components/markdown";
import { DebateExport } from "@/components/debate-export";
import { fetcher } from "@/lib/api";
import { ago, cn } from "@/lib/format";
import type { DebateDetail, JurorVote, Vote } from "@/lib/types";

const VOTE_ORDER: Vote[] = ["BUY", "SELL", "HOLD"];

export default function DebateDetailPage({ params }: { params: { id: string } }) {
  const { data, error, isLoading } = useSWR<DebateDetail>(
    `/api/debate/${encodeURIComponent(params.id)}`,
    fetcher,
  );

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-10 text-sm text-zinc-500">
        <Spinner /> loading debate…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div>
        <BackLink />
        <Card className="border-loss/40">
          <CardBody className="flex items-center gap-2 pt-5 text-sm text-loss">
            <AlertTriangle className="h-4 w-4" />
            {error instanceof Error && error.message.startsWith("404")
              ? `No debate record “${params.id}”.`
              : "Failed to load this debate record."}
          </CardBody>
        </Card>
      </div>
    );
  }

  const isArchive = data.source === "archive";
  const votes = data.jury?.votes ?? [];
  const counts = data.jury?.counts ?? {};

  return (
    <div>
      <BackLink />
      <PageHeader
        title={data.ticker ? `${data.ticker} — Jury Debate` : "Archived Debate"}
        subtitle={data.question}
        right={
          <div className="flex items-center gap-2">
            {data.final_decision && <Badge tone={decisionTone(data.final_decision)}>{data.final_decision}</Badge>}
            <Badge tone="neutral">{data.source}</Badge>
            {data.created_at && <span className="text-xs text-zinc-500">{ago(data.created_at)}</span>}
            <DebateExport data={data} />
          </div>
        }
      />

      {isArchive && data.markdown ? (
        <Card>
          <CardBody className="pt-5">
            <Markdown text={data.markdown} />
          </CardBody>
        </Card>
      ) : (
        <div className="space-y-4">
          {data.jury?.escalated_to_human && (
            <Card className="border-fuchsia-500/40 bg-fuchsia-500/5">
              <CardBody className="pt-5">
                <div className="flex items-center gap-2 font-medium text-fuchsia-300">
                  <AlertTriangle className="h-4 w-4" /> 5-5 Jury Tie — escalated to human
                </div>
                <p className="mt-1 text-sm text-zinc-400">{data.jury.reason} Ties are never auto-resolved.</p>
              </CardBody>
            </Card>
          )}

          {data.fundamentals && (
            <Card>
              <CardHeader>
                <CardTitle>
                  Fundamentals at debate time
                  {data.price != null && <span className="ml-2 font-normal text-zinc-500">live price ${data.price.toFixed(2)}</span>}
                </CardTitle>
              </CardHeader>
              <CardBody>
                <FundamentalsGrid data={data.fundamentals} />
              </CardBody>
            </Card>
          )}

          {data.bull_bear && (
            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle className="text-gain">Bull case</CardTitle>
                </CardHeader>
                <CardBody>
                  <Markdown text={data.bull_bear.bull_case} />
                </CardBody>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle className="text-loss">Bear case</CardTitle>
                </CardHeader>
                <CardBody>
                  <Markdown text={data.bull_bear.bear_case} />
                </CardBody>
              </Card>
            </div>
          )}

          {data.jury && (
            <Card>
              <CardHeader>
                <CardTitle>Jury summary — {votes.length} votes</CardTitle>
              </CardHeader>
              <CardBody className="p-0">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                        <th className="px-5 py-2 font-medium">Vote</th>
                        <th className="px-3 py-2 text-right font-medium">Count</th>
                        <th className="px-5 py-2 font-medium">Jurors</th>
                      </tr>
                    </thead>
                    <tbody>
                      {VOTE_ORDER.map((v) => {
                        const side = votes.filter((j) => j.vote === v);
                        return (
                          <tr key={v} className="border-b border-ink-850 last:border-0">
                            <td className="px-5 py-2.5">
                              <Badge tone={decisionTone(v)}>{v}</Badge>
                            </td>
                            <td className="px-3 py-2.5 text-right tnum text-zinc-300">{counts[v] ?? side.length}</td>
                            <td className="px-5 py-2.5 text-xs text-zinc-500">
                              {side.length > 0
                                ? side.map((j) => `#${j.agent_id} ${j.focus_area.replace(/_/g, " ")}`).join(" · ")
                                : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <div className="border-t border-ink-800 px-5 py-3 text-sm text-zinc-400">
                  <span className="mr-2">
                    <Badge tone={decisionTone(data.jury.decision)}>{data.jury.decision}</Badge>
                  </span>
                  {data.jury.reason}
                </div>
              </CardBody>
            </Card>
          )}

          {votes.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {votes.map((v) => (
                <JurorCard key={v.agent_id} vote={v} />
              ))}
            </div>
          )}

          {data.position_size_note && (
            <Card>
              <CardHeader>
                <CardTitle>Position sizing</CardTitle>
              </CardHeader>
              <CardBody>
                <p className="text-sm leading-relaxed text-zinc-300">{data.position_size_note}</p>
              </CardBody>
            </Card>
          )}

          {data.models && Object.keys(data.models).length > 0 && (
            <p className="text-xs text-zinc-600">
              Models: {Object.entries(data.models).map(([k, m]) => `${k} ${m}`).join(" · ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/debate" className="mb-4 inline-flex items-center gap-1.5 text-sm text-zinc-500 transition-colors hover:text-zinc-200">
      <ArrowLeft className="h-4 w-4" /> All debates
    </Link>
  );
}

function JurorCard({ vote: v }: { vote: JurorVote }) {
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wider text-zinc-500">
          #{v.agent_id} · {v.focus_area.replace(/_/g, " ")}
        </span>
        <span className="flex items-center gap-2">
          <span className="text-xs tnum text-zinc-500">conf {(v.confidence * 100).toFixed(0)}%</span>
          <Badge tone={decisionTone(v.vote)}>{v.vote}</Badge>
        </span>
      </div>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-ink-800">
        <div
          className={cn("h-full rounded-full", v.confidence >= 0.66 ? "bg-gain" : v.confidence >= 0.4 ? "bg-flat" : "bg-loss")}
          style={{ width: `${Math.round(v.confidence * 100)}%` }}
        />
      </div>
      <p className="mt-2 text-sm leading-relaxed text-zinc-400">{v.reasoning}</p>
    </Card>
  );
}
