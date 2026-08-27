"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import useSWR from "swr";
import { Gavel, AlertTriangle, ChevronRight } from "lucide-react";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner, decisionTone } from "@/components/ui";
import { fetcher, streamSSE } from "@/lib/api";
import { ago, cn } from "@/lib/format";
import type { DebateSummary, DebateTurn, JuryResult, JurorVote } from "@/lib/types";

const VOTE_COLOR: Record<string, string> = { BUY: "#34d399", SELL: "#fb7185", HOLD: "#fbbf24" };

export default function DebatePage() {
  const router = useRouter();
  const { data: records, mutate } = useSWR<DebateSummary[]>("/api/debate/records", fetcher, { refreshInterval: 15_000 });
  const [ticker, setTicker] = useState("");
  // The backend has always accepted a `question` (DebateRequest, max 300 chars) and defaulted it to
  // "Should the Agentic account hold {TICKER}?". The page never sent one, so every debate ever run
  // asked the same question. This is the box that changes that.
  const [topic, setTopic] = useState("");
  const [running, setRunning] = useState(false);
  // The exchange, in the order it happened. The engine has been streaming a `turn` event for every
  // opening and rebuttal all along — the page handled six of eleven event types and dropped this
  // one, which is why a debate looked like a post-mortem rather than an argument.
  const [turns, setTurns] = useState<DebateTurn[]>([]);
  const [phase, setPhase] = useState<string | null>(null);
  const [bull, setBull] = useState<string | null>(null);
  const [bear, setBear] = useState<string | null>(null);
  const [votes, setVotes] = useState<JurorVote[]>([]);
  const [jury, setJury] = useState<JuryResult | null>(null);
  const [decision, setDecision] = useState<{ final_decision: string; position_size_note: string; reason: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ctrl = useRef<AbortController | null>(null);

  // Abort any in-flight debate on unmount so the stream stops and wasted Anthropic tokens are
  // bounded — and we don't setState into a dead tree.
  useEffect(() => () => ctrl.current?.abort(), []);

  async function run() {
    const sym = ticker.trim().toUpperCase();
    if (!sym) return;
    ctrl.current?.abort();
    const controller = new AbortController();
    ctrl.current = controller;
    setRunning(true);
    setTurns([]);
    setPhase("opening statements");
    setBull(null);
    setBear(null);
    setVotes([]);
    setJury(null);
    setDecision(null);
    setErr(null);
    try {
      await streamSSE(
        "/api/debate/run-stream",
        // Only send a question when one was typed; an empty string would override the backend's
        // sensible default with nothing.
        topic.trim() ? { ticker: sym, question: topic.trim() } : { ticker: sym },
        (ev) => {
          switch (ev.type) {
            // Five event types the engine emits and the page used to ignore. `turn` is the one that
            // makes a debate watchable; the rest are what tell you it is alive between turns.
            case "debate_start":
              setPhase(`debating ${ev.ticker}`);
              break;
            case "context":
              setPhase("reading price and fundamentals");
              break;
            case "turn":
              setTurns((t) => [...t, ev.turn]);
              setPhase(`round ${ev.turn.round_no} — ${ev.turn.side} ${ev.turn.kind}`);
              break;
            case "notice":
              setPhase(ev.message ?? null);
              break;
            case "debate_complete":
              setPhase(null);
              break;
            case "bull_complete":
              setBull(ev.bull_case);
              break;
            case "bear_complete":
              setBear(ev.bear_case);
              break;
            case "juror_complete":
              setVotes((v) => [...v, ev.vote].sort((a, b) => a.agent_id - b.agent_id));
              setPhase(`jury voting — ${ev.completed}/${ev.total}`);
              break;
            case "aggregate":
              setJury(ev.jury);
              break;
            case "decision":
              setDecision(ev);
              break;
            case "error":
              setErr(ev.message);
              break;
          }
        },
        controller.signal,
      );
    } catch (e) {
      // A user-initiated abort (unmount / new run) is not an error to surface.
      if (e instanceof Error && e.name === "AbortError") return;
      setErr(e instanceof Error ? e.message : "Debate failed");
    } finally {
      if (ctrl.current === controller) {
        setRunning(false);
        setPhase(null);
        ctrl.current = null;
        mutate();
      }
    }
  }

  const counts = jury?.counts ?? deriveCounts(votes);
  const chartData = (["BUY", "SELL", "HOLD"] as const).map((k) => ({ name: k, count: counts[k] ?? 0 }));

  return (
    <div>
      <PageHeader
        title="Jury Debate"
        subtitle="A bull and bear argue it out over several rounds, then 10 jurors judge through their own lens. 6+ decides; a true BUY/SELL deadlock escalates to you."
        right={
          <div className="flex flex-wrap items-center gap-2">
            <input
              aria-label="Ticker"
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              placeholder="ticker e.g. NVDA"
              className="w-32 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm uppercase text-zinc-200 placeholder:text-zinc-600 placeholder:normal-case focus:border-brass focus:outline-none"
            />
            {/* The question the panel argues. Blank falls back to the engine's default,
                "Should the Agentic account hold TICKER?" — which is what every debate on record
                has asked, because the page never sent anything else. */}
            <input
              aria-label="Debate topic"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              maxLength={300}
              placeholder="topic — blank asks: should we hold it?"
              className="w-72 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-brass focus:outline-none"
            />
            <Button variant="brass" onClick={run} disabled={running || !ticker.trim()}>
              {running ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : <Gavel className="h-4 w-4" />}
              {running ? "Debating…" : "Start debate"}
            </Button>
          </div>
        }
      />

      {err && (
        <Card className="mb-4 border-loss/40">
          <CardBody className="flex items-center gap-2 pt-5 text-sm text-loss">
            <AlertTriangle className="h-4 w-4" /> {err}
          </CardBody>
        </Card>
      )}

      {(running || turns.length > 0) && (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle>The exchange</CardTitle>
            {phase && (
              <span className="flex items-center gap-2 text-xs text-zinc-500">
                {running && <Spinner className="h-3 w-3 border-zinc-600 border-t-brass" />}
                {phase}
              </span>
            )}
          </CardHeader>
          <CardBody className="space-y-3">
            {turns.length === 0 && running && (
              <p className="text-sm text-zinc-500">Both sides are writing their opening cases…</p>
            )}
            {turns.map((t, i) => (
              <div
                key={`${t.round_no}-${t.side}-${i}`}
                className={cn(
                  "rounded-lg border-l-2 bg-ink-900/60 px-4 py-3",
                  t.side === "bull" ? "border-gain/60" : "border-loss/60",
                )}
              >
                <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wider">
                  <span className={t.side === "bull" ? "text-gain" : "text-loss"}>{t.side}</span>
                  <span className="text-zinc-600">
                    round {t.round_no} · {t.kind}
                  </span>
                </div>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-300">{t.content}</p>
              </div>
            ))}
          </CardBody>
        </Card>
      )}

      {/* #139: a panel whose confidences are one number wearing a distribution's clothes. Shown
          because the audience for "your jury is not discriminating" is whoever is deciding whether
          to trust the verdict. */}
      {(jury?.calibration_signals?.length ?? 0) > 0 && (
        <Card className="mb-4 border-flat/40 bg-flat/5">
          <CardBody className="pt-5">
            <div className="flex items-center gap-2 font-medium text-flat">
              <AlertTriangle className="h-4 w-4" /> Read this jury with suspicion
            </div>
            <ul className="mt-2 space-y-1 text-sm text-zinc-400">
              {jury!.calibration_signals!.map((sig: string, i: number) => (
                <li key={i}>· {sig}</li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}

      {jury?.escalated_to_human && (
        <Card className="mb-4 border-fuchsia-500/40 bg-fuchsia-500/5">
          <CardBody className="pt-5">
            <div className="flex items-center gap-2 font-medium text-fuchsia-300">
              <AlertTriangle className="h-4 w-4" /> 5-5 Jury Tie — escalated to human
            </div>
            <p className="mt-1 text-sm text-zinc-400">{jury.reason} Ties are never auto-resolved.</p>
          </CardBody>
        </Card>
      )}

      {(votes.length > 0 || bull || decision) && (
        <div className="mb-4 grid gap-4 lg:grid-cols-[1fr_300px]">
          <div className="grid gap-4 sm:grid-cols-2">
            <CasePanel title="Bull case" tone="text-gain" body={bull} running={running && !bull} />
            <CasePanel title="Bear case" tone="text-loss" body={bear} running={running && !bear} />
          </div>
          <Card>
            <CardHeader>
              <CardTitle>Vote tally</CardTitle>
            </CardHeader>
            <CardBody>
              {decision && (
                <div className="mb-3 flex items-center gap-2">
                  <Badge tone={decisionTone(decision.final_decision)}>{decision.final_decision}</Badge>
                  <span className="text-xs text-zinc-500">{votes.length}/10 voted</span>
                </div>
              )}
              <div className="h-40">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData}>
                    <XAxis dataKey="name" stroke="#71717a" fontSize={12} tickLine={false} axisLine={false} />
                    <YAxis allowDecimals={false} stroke="#71717a" fontSize={12} width={20} tickLine={false} axisLine={false} />
                    <Tooltip cursor={{ fill: "#23262d" }} contentStyle={{ background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 }} />
                    <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                      {chartData.map((d) => (
                        <Cell key={d.name} fill={VOTE_COLOR[d.name]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              {decision && <p className="mt-3 text-xs leading-relaxed text-zinc-500">{decision.position_size_note}</p>}
            </CardBody>
          </Card>
        </div>
      )}

      {votes.length > 0 && (
        <div className="mb-6 grid gap-3 sm:grid-cols-2">
          {votes.map((v) => (
            <Card key={v.agent_id} className="px-4 py-3">
              <div className="flex items-center justify-between">
                <span className="text-xs uppercase tracking-wider text-zinc-500">
                  #{v.agent_id} · {v.focus_area.replace(/_/g, " ")}
                </span>
                <Badge tone={decisionTone(v.vote)}>{v.vote}</Badge>
              </div>
              {/* Drawn only when the panel's confidences carry information. `usable` is false when
                  they are effectively one repeated number — measured at 0.72 on 59% of every vote
                  ever cast — and a bar drawn from a constant asserts a measurement nothing made. */}
              {jury?.confidence?.usable === false ? (
                <div className="mt-2 text-xs text-zinc-600">
                  confidence {v.confidence.toFixed(2)} — not shown as a bar: the panel returned
                  effectively one value
                </div>
              ) : (
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-ink-800">
                  <div
                    className={cn("h-full rounded-full", v.confidence >= 0.66 ? "bg-gain" : v.confidence >= 0.4 ? "bg-flat" : "bg-loss")}
                    style={{ width: `${Math.round(v.confidence * 100)}%` }}
                  />
                </div>
              )}
              <p className="mt-2 text-sm leading-snug text-zinc-400">{v.reasoning}</p>
            </Card>
          ))}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Debate history</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          <table className="w-full text-sm">
            <tbody>
              {(records ?? []).map((r) => (
                // The whole row navigates (big click target); the question is also a real <Link>
                // so keyboard users can tab to it and middle-click/open-in-new-tab works.
                <tr
                  key={r.id}
                  onClick={() => router.push(`/debate/${encodeURIComponent(r.id)}`)}
                  className="cursor-pointer border-b border-ink-850 transition-colors last:border-0 hover:bg-ink-850/60"
                >
                  <td className="px-5 py-2.5 text-zinc-300">
                    <Link
                      href={`/debate/${encodeURIComponent(r.id)}`}
                      onClick={(e) => e.stopPropagation()}
                      className="hover:text-zinc-100"
                    >
                      {r.question}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5">{r.decision && <Badge tone={decisionTone(r.decision)}>{r.decision}</Badge>}</td>
                  <td className="px-3 py-2.5">
                    <Badge tone="neutral">{r.source}</Badge>
                  </td>
                  <td className="px-5 py-2.5 text-right text-xs text-zinc-500">{ago(r.created_at)}</td>
                  <td className="py-2.5 pr-4 text-right">
                    <ChevronRight className="ml-auto h-4 w-4 text-zinc-600" />
                  </td>
                </tr>
              ))}
              {(!records || records.length === 0) && (
                <tr>
                  <td className="px-5 py-6 text-sm text-zinc-500">No debates yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </CardBody>
      </Card>
    </div>
  );
}

function CasePanel({ title, tone, body, running }: { title: string; tone: string; body: string | null; running: boolean }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className={tone}>{title}</CardTitle>
      </CardHeader>
      <CardBody>
        {body ? (
          <p className="text-sm leading-relaxed text-zinc-300">{body}</p>
        ) : running ? (
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Spinner /> building…
          </div>
        ) : (
          <p className="text-sm text-zinc-600">—</p>
        )}
      </CardBody>
    </Card>
  );
}

function deriveCounts(votes: JurorVote[]): Record<string, number> {
  const c: Record<string, number> = { BUY: 0, SELL: 0, HOLD: 0 };
  for (const v of votes) c[v.vote] = (c[v.vote] ?? 0) + 1;
  return c;
}
