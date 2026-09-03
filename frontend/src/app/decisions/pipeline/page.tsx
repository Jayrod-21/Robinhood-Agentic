"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import useSWR from "swr";
import { Activity, Check, ChevronRight, Circle, Loader2, X } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner, decisionTone } from "@/components/ui";
import { fetcher, streamSSE } from "@/lib/api";
import { ago, cn, pct, plColor, usd } from "@/lib/format";
import type { JurorVote, PipelineRunView } from "@/lib/types";

type NodeStatus = "pending" | "running" | "completed" | "error";
const NODE_LABELS: Record<string, string> = {
  screen: "Sprinkle Sauce screen",
  bull: "Bull researcher",
  bear: "Bear researcher",
  jury: "10-agent jury",
  decision: "Decision arbiter",
};
const ORDER = ["screen", "bull", "bear", "jury", "decision"];

interface NodeState {
  status: NodeStatus;
  data?: any;
  votes?: JurorVote[];
}

export default function PipelinePage() {
  const [ticker, setTicker] = useState("");
  const [running, setRunning] = useState(false);
  const [nodes, setNodes] = useState<Record<string, NodeState>>({});
  const [err, setErr] = useState<string | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  // Run history (issue #28) — refreshes on an interval so the "now" column tracks the live mark,
  // and is revalidated right after a run completes so the new row appears without a reload.
  const { data: history, mutate: mutateHistory } = useSWR<PipelineRunView[]>(
    "/api/pipeline/history",
    fetcher,
    { refreshInterval: 30_000 },
  );

  // Abort any in-flight stream on unmount so the reader stops and we don't setState into a dead tree.
  useEffect(() => () => ctrl.current?.abort(), []);

  function setNode(name: string, patch: Partial<NodeState>) {
    setNodes((n) => ({ ...n, [name]: { ...(n[name] ?? { status: "pending" }), ...patch } }));
  }

  // Flip every node that hasn't completed to "error" so a backend failure shows on the stepper
  // instead of leaving the active node spinning forever.
  function failPendingNodes() {
    setNodes((n) =>
      Object.fromEntries(
        Object.entries(n).map(([k, v]) =>
          v.status === "completed" ? [k, v] : [k, { ...v, status: "error" as NodeStatus }],
        ),
      ),
    );
  }

  async function run() {
    const sym = ticker.trim().toUpperCase();
    if (!sym) return;
    // Abort any previous run before starting a new one.
    ctrl.current?.abort();
    const controller = new AbortController();
    ctrl.current = controller;
    setRunning(true);
    setErr(null);
    setNodes(Object.fromEntries(ORDER.map((n) => [n, { status: "pending" as NodeStatus }])));
    try {
      await streamSSE(
        "/api/pipeline/run-stream",
        { ticker: sym },
        (ev) => {
          switch (ev.type) {
            case "node_start":
              setNode(ev.node, { status: "running" });
              break;
            case "node_progress":
              setNodes((n) => {
                const cur = n[ev.node] ?? { status: "running" as NodeStatus };
                const votes = [...(cur.votes ?? []), ev.vote].sort((a, b) => a.agent_id - b.agent_id);
                return { ...n, [ev.node]: { ...cur, status: "running", votes } };
              });
              break;
            case "node_complete":
              setNode(ev.node, { status: "completed", data: ev.data });
              break;
            case "pipeline_error":
              setErr(ev.message);
              failPendingNodes();
              break;
          }
        },
        controller.signal,
      );
    } catch (e) {
      // A user-initiated abort (unmount / new run) is not an error to surface.
      if (e instanceof Error && e.name === "AbortError") return;
      setErr(e instanceof Error ? e.message : "Pipeline failed");
      failPendingNodes();
    } finally {
      if (ctrl.current === controller) {
        setRunning(false);
        ctrl.current = null;
        mutateHistory();
      }
    }
  }

  return (
    <div>
      <PageHeader
        title="Decision Pipeline"
        subtitle="One ticker through the full chain: real screen → bull/bear → live jury → sized decision."
        right={
          <div className="flex items-center gap-2">
            <input
              aria-label="Ticker"
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              placeholder="ticker e.g. NVDA"
              className="w-40 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm uppercase text-zinc-200 placeholder:text-zinc-600 placeholder:normal-case focus:border-brass focus:outline-none"
            />
            <Button variant="brass" onClick={run} disabled={running || !ticker.trim()}>
              {running ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : <Activity className="h-4 w-4" />}
              {running ? "Running…" : "Run pipeline"}
            </Button>
          </div>
        }
      />

      {err && (
        <Card className="mb-4 border-loss/40">
          <CardBody className="pt-5 text-sm text-loss">{err}</CardBody>
        </Card>
      )}

      {Object.keys(nodes).length === 0 ? (
        <Card>
          <CardBody className="py-10 text-center text-sm text-zinc-500">Enter a ticker and run the pipeline to watch each node execute.</CardBody>
        </Card>
      ) : (
        <ol className="relative ml-3 border-l border-ink-800">
          {ORDER.map((name) => {
            const node = nodes[name] ?? { status: "pending" as NodeStatus };
            return (
              <li key={name} className="mb-4 ml-6">
                <span className="absolute -left-3 flex h-6 w-6 items-center justify-center rounded-full border border-ink-700 bg-ink-900">
                  <NodeIcon status={node.status} />
                </span>
                <Card className={cn("ml-2", node.status === "running" && "border-brass/40")}>
                  <CardBody className="py-3">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-zinc-200">{NODE_LABELS[name]}</span>
                      <NodeBadge name={name} node={node} />
                    </div>
                    <NodeDetail name={name} node={node} />
                  </CardBody>
                </Card>
              </li>
            );
          })}
        </ol>
      )}

      <Card className="mt-6">
        <CardHeader>
          <CardTitle>Run history</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          <RunHistory runs={history} />
        </CardBody>
      </Card>
    </div>
  );
}

/** Every persisted pipeline run: price at run vs the current mark, in dollars and percent
 *  (issue #28). Rows link to the wrapped debate's stage-by-stage detail when one was recorded. */
function RunHistory({ runs }: { runs: PipelineRunView[] | undefined }) {
  const router = useRouter();

  if (!runs) {
    return (
      <div className="flex items-center gap-2 px-5 py-6 text-sm text-zinc-500">
        <Spinner /> loading history…
      </div>
    );
  }
  if (runs.length === 0) {
    return <p className="px-5 py-6 text-sm text-zinc-500">No pipeline runs recorded yet — run one above.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
            <th className="px-5 py-2 font-medium">Ticker</th>
            <th className="px-3 py-2 font-medium">Decision</th>
            <th className="px-3 py-2 font-medium">Screen</th>
            <th className="px-3 py-2 text-right font-medium">At run</th>
            <th className="px-3 py-2 text-right font-medium">Now</th>
            <th className="px-3 py-2 text-right font-medium">Δ</th>
            <th className="px-3 py-2 text-right font-medium">Δ%</th>
            <th className="px-5 py-2 text-right font-medium">When</th>
            <th className="py-2 pr-4" />
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const detailHref = r.debate_id ? `/debate/${encodeURIComponent(r.debate_id)}` : null;
            return (
              // Same pattern as the debate history table: the whole row navigates (big click
              // target) and the ticker is also a real <Link> for keyboard / open-in-new-tab.
              <tr
                key={r.id}
                onClick={() => detailHref && router.push(detailHref)}
                className={cn(
                  "border-b border-ink-850 transition-colors last:border-0",
                  detailHref && "cursor-pointer hover:bg-ink-850/60",
                )}
              >
                <td className="px-5 py-2.5 font-medium text-zinc-200">
                  {detailHref ? (
                    <Link href={detailHref} onClick={(e) => e.stopPropagation()} className="hover:text-zinc-50">
                      {r.ticker}
                    </Link>
                  ) : (
                    r.ticker
                  )}
                </td>
                <td className="px-3 py-2.5">
                  {r.decision ? <Badge tone={decisionTone(r.decision)}>{r.decision}</Badge> : <span className="text-zinc-600">—</span>}
                </td>
                <td className="px-3 py-2.5">
                  {r.screen_passed == null ? (
                    <span className="text-zinc-600">—</span>
                  ) : r.screen_passed ? (
                    <Badge tone="buy">PASS</Badge>
                  ) : (
                    <Badge tone="sell" title={r.screen_reason ?? undefined}>FAIL</Badge>
                  )}
                </td>
                <td className="px-3 py-2.5 text-right tnum text-zinc-300">{usd(r.price_at_run)}</td>
                <td className="px-3 py-2.5 text-right tnum text-zinc-300">{usd(r.current_price)}</td>
                <td className={cn("px-3 py-2.5 text-right tnum", plColor(r.delta))}>
                  {r.delta != null && r.delta > 0 ? "+" : ""}
                  {usd(r.delta)}
                </td>
                <td className={cn("px-3 py-2.5 text-right tnum", plColor(r.delta_pct))}>{pct(r.delta_pct)}</td>
                <td className="px-5 py-2.5 text-right text-xs text-zinc-500">{ago(r.created_at)}</td>
                <td className="py-2.5 pr-4 text-right">
                  {detailHref && <ChevronRight className="ml-auto h-4 w-4 text-zinc-600" />}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function NodeIcon({ status }: { status: NodeStatus }) {
  if (status === "completed") return <Check className="h-3.5 w-3.5 text-gain" />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin text-brass" />;
  if (status === "error") return <X className="h-3.5 w-3.5 text-loss" />;
  return <Circle className="h-2.5 w-2.5 text-zinc-600" />;
}

function NodeBadge({ name, node }: { name: string; node: NodeState }) {
  if (name === "screen" && node.data) {
    return node.data.passed ? <Badge tone="buy">PASS</Badge> : <Badge tone="sell">{node.data.reason ?? "FAIL"}</Badge>;
  }
  if (name === "decision" && node.data?.final_decision) {
    return <Badge tone={decisionTone(node.data.final_decision)}>{node.data.final_decision}</Badge>;
  }
  if (name === "jury") {
    const c = node.data?.counts;
    if (c) return <span className="text-xs text-zinc-400 tnum">BUY {c.BUY} · SELL {c.SELL} · HOLD {c.HOLD}</span>;
    if (node.votes) return <span className="text-xs text-zinc-500">{node.votes.length}/10</span>;
  }
  return null;
}

function NodeDetail({ name, node }: { name: string; node: NodeState }) {
  if (name === "screen" && node.data) {
    return (
      <p className="mt-1 text-xs text-zinc-500 tnum">
        {node.data.price != null && <>price {usd(node.data.price)} · </>}
        {node.data.composite != null ? <>composite {node.data.composite.toFixed(3)}</> : node.data.reason}
      </p>
    );
  }
  if ((name === "bull" || name === "bear") && node.data) {
    return <p className="mt-1 text-sm leading-snug text-zinc-400">{node.data.bull_case ?? node.data.bear_case}</p>;
  }
  if (name === "jury" && (node.votes?.length || node.data)) {
    const votes = (node.data?.votes ?? node.votes ?? []) as JurorVote[];
    return (
      <div className="mt-2 flex flex-wrap gap-1.5">
        {votes.map((v) => (
          <span
            key={v.agent_id}
            title={`#${v.agent_id} ${v.focus_area}: ${v.reasoning}`}
            className={cn(
              "rounded px-1.5 py-0.5 text-[11px] font-medium",
              v.vote === "BUY" && "bg-gain/15 text-gain",
              v.vote === "SELL" && "bg-loss/15 text-loss",
              v.vote === "HOLD" && "bg-flat/15 text-flat",
            )}
          >
            {v.vote[0]}
          </span>
        ))}
      </div>
    );
  }
  if (name === "decision" && node.data) {
    return <p className="mt-1 text-xs leading-relaxed text-zinc-500">{node.data.position_size_note ?? node.data.reason}</p>;
  }
  return null;
}
