"use client";

import { useEffect, useRef, useState } from "react";
import { Play, CheckCircle2, XCircle, AlertTriangle, ChevronDown } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner } from "@/components/ui";
import { FundamentalsGrid, compactUsd } from "@/components/fundamentals";
import { streamSSE } from "@/lib/api";
import { cn } from "@/lib/format";
import type { ScanResult } from "@/lib/types";

export default function ScanPage() {
  const [running, setRunning] = useState(false);
  const [tickersInput, setTickersInput] = useState("");
  const [results, setResults] = useState<ScanResult[]>([]);
  const [survivors, setSurvivors] = useState<ScanResult[] | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number }>({ done: 0, total: 0 });
  const [err, setErr] = useState<string | null>(null);
  // Tickers whose row is expanded to show the full fundamentals (issue #27). Cleared per run.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const ctrl = useRef<AbortController | null>(null);

  function toggleExpanded(ticker: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(ticker)) next.delete(ticker);
      else next.add(ticker);
      return next;
    });
  }

  // Abort any in-flight scan on unmount so the reader stops and we don't setState into a dead tree.
  useEffect(() => () => ctrl.current?.abort(), []);

  async function run() {
    ctrl.current?.abort();
    const controller = new AbortController();
    ctrl.current = controller;
    setRunning(true);
    setResults([]);
    setSurvivors(null);
    setErr(null);
    setExpanded(new Set());
    setProgress({ done: 0, total: 0 });
    const tickers = tickersInput
      .split(/[\s,]+/)
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean);
    try {
      await streamSSE(
        "/api/scan/run-stream",
        tickers.length ? { tickers } : {},
        (ev) => {
          if (ev.type === "scan_start") setProgress({ done: 0, total: ev.count });
          else if (ev.type === "scan_result") {
            setResults((r) => [...r, ev.result]);
            setProgress({ done: ev.index + 1, total: ev.total });
          } else if (ev.type === "scan_complete") setSurvivors(ev.survivors);
        },
        controller.signal,
      );
    } catch (e) {
      // A user-initiated abort (unmount / new run) is not an error to surface.
      if (e instanceof Error && e.name === "AbortError") return;
      setErr(e instanceof Error ? e.message : "Scan failed");
    } finally {
      if (ctrl.current === controller) {
        setRunning(false);
        ctrl.current = null;
      }
    }
  }

  return (
    <div>
      <PageHeader
        title="Sprinkle Sauce Scan"
        subtitle="Live fundamental screen over the seed universe — yfinance → tiered gates → composite rank. No LLM, no cost."
        right={
          <div className="flex items-center gap-2">
            <input
              aria-label="Tickers to scan"
              value={tickersInput}
              onChange={(e) => setTickersInput(e.target.value)}
              placeholder="optional: AAPL NVDA OXY"
              className="w-56 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-brass focus:outline-none"
            />
            <Button variant="brass" onClick={run} disabled={running}>
              {running ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : <Play className="h-4 w-4" />}
              {running ? `Scanning ${progress.done}/${progress.total}` : "Run scan"}
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

      {survivors && (
        <Card className="mb-4 border-gain/30">
          <CardHeader>
            <CardTitle>Survivors — ranked pre-Wasden ({survivors.length})</CardTitle>
          </CardHeader>
          <CardBody className="flex flex-wrap gap-2">
            {survivors.length === 0 && <span className="text-sm text-zinc-500">No name cleared the gates today.</span>}
            {survivors.map((s) => (
              <span key={s.ticker} className="rounded-lg border border-gain/30 bg-gain/10 px-3 py-1.5 text-sm">
                <span className="font-medium text-zinc-100">{s.ticker}</span>
                <span className="ml-2 tnum text-gain">{s.composite?.toFixed(3)}</span>
              </span>
            ))}
          </CardBody>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Results {progress.total > 0 && `(${results.length}/${progress.total})`}</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {results.length === 0 ? (
            <div className="px-5 py-8 text-sm text-zinc-500">Run a scan to screen the universe.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-y border-ink-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                    <th className="px-5 py-2 font-medium">Ticker</th>
                    <th className="px-3 py-2 font-medium">Verdict</th>
                    <th className="px-3 py-2 text-right font-medium">Price</th>
                    <th className="px-3 py-2 text-right font-medium">Mkt cap</th>
                    <th className="px-3 py-2 text-right font-medium">PEG</th>
                    <th className="px-3 py-2 text-right font-medium">FCF yld</th>
                    <th className="px-3 py-2 text-right font-medium">Score</th>
                    <th className="px-5 py-2 font-medium">Note</th>
                    <th className="w-10 py-2 pr-4" aria-label="Expand" />
                  </tr>
                </thead>
                <tbody>
                  {results.map((r) => {
                    const open = expanded.has(r.ticker);
                    return (
                      <ResultRow key={r.ticker} r={r} open={open} onToggle={() => toggleExpanded(r.ticker)} />
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

/** One scan result plus its expandable full-fundamentals detail row (issue #27). */
function ResultRow({ r, open, onToggle }: { r: ScanResult; open: boolean; onToggle: () => void }) {
  return (
    <>
      <tr
        onClick={onToggle}
        className={cn(
          "cursor-pointer border-b transition-colors hover:bg-ink-850/60",
          open ? "border-transparent bg-ink-850/40" : "border-ink-850 last:border-0",
        )}
      >
        <td className="px-5 py-2.5">
          <span className="font-medium text-zinc-100">{r.ticker}</span>
          {r.name && <span className="ml-2 text-xs text-zinc-500">{r.name}</span>}
        </td>
        <td className="px-3 py-2.5">
          {r.passed ? (
            <Badge tone="buy">
              <CheckCircle2 className="mr-1 h-3 w-3" /> PASS
            </Badge>
          ) : (
            <Badge tone={r.ok ? "sell" : "neutral"}>
              <XCircle className="mr-1 h-3 w-3" /> {r.ok ? "FAIL" : "NO DATA"}
            </Badge>
          )}
        </td>
        <td className="px-3 py-2.5 text-right tnum text-zinc-300">{r.price != null ? `$${r.price.toFixed(2)}` : "—"}</td>
        <td className="px-3 py-2.5 text-right tnum text-zinc-300">{compactUsd(r.market_cap)}</td>
        <td className="px-3 py-2.5 text-right tnum text-zinc-300">{r.peg?.toFixed(2) ?? "—"}</td>
        <td className="px-3 py-2.5 text-right tnum text-zinc-300">{r.fcf_yield != null ? `${r.fcf_yield.toFixed(1)}%` : "—"}</td>
        <td className={cn("px-3 py-2.5 text-right tnum", r.passed ? "text-gain" : "text-zinc-600")}>
          {r.composite?.toFixed(3) ?? "—"}
        </td>
        <td className="px-5 py-2.5 text-xs text-zinc-500">{r.reason ?? "—"}</td>
        <td className="py-2.5 pr-4 text-right">
          <button
            type="button"
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${r.ticker} fundamentals`}
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
            className="rounded p-1 text-zinc-600 transition-colors hover:bg-ink-800 hover:text-zinc-300"
          >
            <ChevronDown className={cn("h-4 w-4 transition-transform", open && "rotate-180")} />
          </button>
        </td>
      </tr>
      {open && (
        <tr className="border-b border-ink-850 bg-ink-850/40 last:border-0">
          <td colSpan={9} className="px-5 pb-4 pt-1">
            {r.ok ? (
              <FundamentalsGrid data={r} />
            ) : (
              <p className="text-xs text-zinc-500">No fundamentals — yfinance returned nothing usable for {r.ticker}.</p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
