"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { ArrowDown, ArrowUp, Check, Database, Minus, Search, X } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/format";
import {
  ANNUAL_GROUPS,
  KNOWN_ABSENT,
  MARKET_FIELDS,
  PIOTROSKI_LABELS,
  type AnnualPeriod,
  type FieldSpec,
  type FundamentalsResponse,
  type FundamentalsRow,
  type Unit,
} from "@/lib/fundamentals";

// ── formatting ────────────────────────────────────────────────────────────────────────────────
// One formatter per unit, chosen from the field spec rather than inferred. A fraction rendered
// with the ratio formatter reads as "0.47" gross margin; the same number through the percent
// formatter reads as "46.9%". Only the field spec knows which is meant.

function fmt(value: number | string | null | undefined, unit: Unit): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  switch (unit) {
    case "usd":
      return `$${value.toFixed(2)}`;
    case "usdCompact": {
      const abs = Math.abs(value);
      const sign = value < 0 ? "-" : "";
      if (abs >= 1e12) return `${sign}$${(abs / 1e12).toFixed(2)}T`;
      if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
      if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
      return `${sign}$${abs.toFixed(0)}`;
    }
    case "fraction":
      return `${(value * 100).toFixed(1)}%`;
    case "ratio":
      return `${value.toFixed(2)}×`;
    case "days":
      return `${value.toFixed(0)}d`;
    case "shares":
      return value >= 1e9 ? `${(value / 1e9).toFixed(2)}B` : `${(value / 1e6).toFixed(1)}M`;
    case "int":
      return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }
}

/** F-score colouring follows the literature's own reading: 8-9 strong, 0-2 weak. */
function scoreTone(score: number | null): string {
  if (score === null) return "text-zinc-500";
  if (score >= 8) return "text-gain";
  if (score >= 5) return "text-zinc-200";
  return "text-loss";
}

// ── Piotroski ─────────────────────────────────────────────────────────────────────────────────

function PiotroskiBlock({ period }: { period: AnnualPeriod }) {
  const p = period.piotroski_signals;
  if (!p) {
    return (
      <p className="text-sm text-zinc-500">
        No F-score for this period. The score compares a year against the one before it, so the
        oldest period on record has nothing to compare to.
      </p>
    );
  }
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={cn("text-3xl font-semibold tnum", scoreTone(period.piotroski_f_score))}>
          {p.score}
        </span>
        <span className="text-sm text-zinc-400">
          of {p.evaluated} signals evaluated{p.evaluated !== p.of && ` (${p.of - p.evaluated} unknown)`}
        </span>
        <Badge tone={p.complete ? "buy" : "hold"}>
          {p.complete ? `complete · ${p.variant}` : `partial · ${p.variant}`}
        </Badge>
      </div>
      {!p.complete && (
        // The stored f_score column is NULL whenever a signal could not be computed, because a
        // "2 out of 8" is not comparable to a "2 out of 9". The partial tally is still shown —
        // hiding it would waste the eight signals that DID compute — but it is labelled as partial
        // rather than presented as an F-score.
        <p className="mt-1 text-xs text-zinc-500">
          Not stored as an F-score: a tally over {p.evaluated} signals is not comparable to one over{" "}
          {p.of}. The signals below say which input was missing.
        </p>
      )}
      <p className="mt-1 text-xs text-zinc-500">
        {p.periods.prior ?? "?"} → {p.periods.current ?? "?"}
      </p>
      <ul className="mt-3 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(PIOTROSKI_LABELS).map(([key, label]) => {
          const v = p.signals[key];
          return (
            <li key={key} className="flex items-center gap-2 text-sm">
              {v === true && <Check className="h-3.5 w-3.5 shrink-0 text-gain" />}
              {v === false && <X className="h-3.5 w-3.5 shrink-0 text-loss" />}
              {v == null && <Minus className="h-3.5 w-3.5 shrink-0 text-zinc-600" />}
              <span className={cn(v == null ? "text-zinc-500" : "text-zinc-300")}>{label}</span>
              {v == null && <span className="text-xs text-zinc-600">(inputs missing)</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ── history ───────────────────────────────────────────────────────────────────────────────────

/** Direction of travel between two periods, when the field has a defensible "better". */
function Trend({ spec, newer, older }: { spec: FieldSpec; newer: number | null; older: number | null }) {
  if (spec.higherIsBetter === undefined || newer == null || older == null || newer === older) {
    return null;
  }
  const up = newer > older;
  const good = up === spec.higherIsBetter;
  const Icon = up ? ArrowUp : ArrowDown;
  return <Icon className={cn("ml-1 inline h-3 w-3", good ? "text-gain" : "text-loss")} />;
}

function HistoryTable({ history }: { history: AnnualPeriod[] }) {
  // Oldest first reads left-to-right as time passing, which is how a trend is read.
  const periods = useMemo(() => [...history].reverse(), [history]);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b border-ink-700 text-left text-xs uppercase tracking-wider text-zinc-500">
            <th className="py-2 pr-4 font-medium">Metric</th>
            {periods.map((p) => (
              <th key={p.period_end} className="py-2 pr-4 text-right font-medium tnum">
                {p.period_end.slice(0, 4)}
                <div className="font-normal normal-case tracking-normal text-[10px] text-zinc-600">
                  filed {p.known_at.slice(0, 10)}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr className="border-b border-ink-800">
            <td className="py-2 pr-4 text-zinc-300">Piotroski (Cary)</td>
            {periods.map((p) => (
              <td key={p.period_end} className={cn("py-2 pr-4 text-right tnum", scoreTone(p.piotroski_f_score))}>
                {p.piotroski_f_score ?? (p.piotroski_signals ? `${p.piotroski_signals.score}*` : "—")}
              </td>
            ))}
          </tr>
          {ANNUAL_GROUPS.flatMap((g) =>
            g.fields.map((spec) => (
              <tr key={spec.key} className="border-b border-ink-800/60">
                <td className="py-1.5 pr-4 text-zinc-400">{spec.label}</td>
                {periods.map((p, i) => {
                  const v = p[spec.key as keyof AnnualPeriod] as number | null;
                  const prev = i > 0 ? (periods[i - 1][spec.key as keyof AnnualPeriod] as number | null) : null;
                  return (
                    <td key={p.period_end} className="py-1.5 pr-4 text-right tnum text-zinc-300">
                      {fmt(v, spec.unit)}
                      <Trend spec={spec} newer={v} older={prev} />
                    </td>
                  );
                })}
              </tr>
            )),
          )}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-zinc-600">
        An asterisk marks a partial tally: some signals had no inputs, so it is not stored as an
        F-score. Filing dates are the acceptance timestamps each row is dated by.
      </p>
    </div>
  );
}

// ── detail ────────────────────────────────────────────────────────────────────────────────────

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="mt-0.5 text-sm tnum text-zinc-200">{value}</div>
    </div>
  );
}

function Detail({ row }: { row: FundamentalsRow }) {
  const annual = row.latest_annual;
  const derived = annual?.derived_fields ?? {};
  return (
    <div className="space-y-4">
      {row.meta_note && (
        <Card>
          <CardBody className="flex items-start gap-3 pt-5 text-sm text-zinc-400">
            <Database className="mt-0.5 h-4 w-4 shrink-0" />
            {row.meta_note}
          </CardBody>
        </Card>
      )}

      {row.market && (
        <Card>
          <CardHeader>
            <CardTitle>Market</CardTitle>
            <span className="text-xs text-zinc-500">as of {row.market.known_at.slice(0, 10)}</span>
          </CardHeader>
          <CardBody className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4 lg:grid-cols-5">
            {MARKET_FIELDS.map((spec) => (
              <Field
                key={spec.key}
                label={spec.label}
                value={fmt(row.market![spec.key as keyof typeof row.market] as number | null, spec.unit)}
              />
            ))}
            <Field label="Analyst view" value={row.market.analyst_recommendation ?? "—"} />
          </CardBody>
        </Card>
      )}

      {annual ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Piotroski F-Score</CardTitle>
              <span className="text-xs text-zinc-500">
                FY{annual.period_end.slice(0, 4)} · filed {annual.known_at.slice(0, 10)}
              </span>
            </CardHeader>
            <CardBody>
              <PiotroskiBlock period={annual} />
            </CardBody>
          </Card>

          {ANNUAL_GROUPS.map((group) => (
            <Card key={group.title}>
              <CardHeader>
                <CardTitle>{group.title}</CardTitle>
                <span className="text-xs text-zinc-500">FY{annual.period_end.slice(0, 4)}</span>
              </CardHeader>
              <CardBody className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-4">
                {group.fields.map((spec) => (
                  <Field
                    key={spec.key}
                    label={spec.label + (derived[spec.key] ? " ·" : "")}
                    value={fmt(annual[spec.key as keyof AnnualPeriod] as number | null, spec.unit)}
                  />
                ))}
              </CardBody>
            </Card>
          ))}

          {row.history.length > 1 && (
            <Card>
              <CardHeader>
                <CardTitle>History</CardTitle>
                <span className="text-xs text-zinc-500">{row.periods_on_record} filed periods</span>
              </CardHeader>
              <CardBody>
                <HistoryTable history={row.history} />
              </CardBody>
            </Card>
          )}

          {Object.keys(derived).length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Calculated here</CardTitle>
                <span className="text-xs text-zinc-500">not supplied by the data vendor</span>
              </CardHeader>
              <CardBody className="space-y-1 text-sm">
                {Object.entries(derived).map(([field, formula]) => (
                  <div key={field} className="flex flex-wrap gap-x-2">
                    <span className="text-zinc-300">{field}</span>
                    <span className="text-zinc-500">= {formula}</span>
                  </div>
                ))}
              </CardBody>
            </Card>
          )}
        </>
      ) : (
        <Card>
          <CardBody className="pt-5 text-sm text-zinc-400">
            No filed statements on record for {row.symbol}. Funds and ETFs have none to file, which
            is the usual reason.
          </CardBody>
        </Card>
      )}
    </div>
  );
}

// ── page ──────────────────────────────────────────────────────────────────────────────────────

export default function FundamentalsPage() {
  const [extra, setExtra] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  // No `symbols` param means the endpoint answers with what the account holds, which is the view
  // an owner wants first. Typing a symbol widens it to anything in the securities table.
  const path = query ? `/api/fundamentals?symbols=${encodeURIComponent(query)}` : "/api/fundamentals";
  const { data, error, isLoading } = useSWR<FundamentalsResponse>(path, fetcher);

  const rows = data?.rows ?? [];
  const active = rows.find((r) => r.symbol === selected) ?? null;

  const header = (
    <PageHeader
      title="Fundamentals"
      subtitle={
        data
          ? `${data.meta.count} companies · Piotroski variant: ${data.meta.piotroski_variant}`
          : "Full fundamental set with filed history"
      }
    />
  );

  if (error) {
    return (
      <div>
        {header}
        <Card className="border-loss/40">
          <CardBody className="pt-5 text-sm text-zinc-400">
            Fundamentals are unavailable: {String(error.message ?? error)}
          </CardBody>
        </Card>
      </div>
    );
  }

  return (
    <div>
      {header}

      <form
        className="mb-4 flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(extra.trim().toUpperCase());
          setSelected(null);
        }}
      >
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-zinc-500" />
          <input
            value={extra}
            onChange={(e) => setExtra(e.target.value)}
            placeholder="Symbols, comma separated"
            className="w-64 rounded-md border border-ink-700 bg-ink-900 py-2 pl-8 pr-3 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-zinc-500 focus:outline-none"
          />
        </div>
        {query && (
          <button
            type="button"
            onClick={() => { setExtra(""); setQuery(""); setSelected(null); }}
            className="text-xs text-zinc-500 hover:text-zinc-300"
          >
            back to holdings
          </button>
        )}
      </form>

      {data && data.meta.unknown_symbols.length > 0 && (
        // Named, because a symbol absent from `securities` looks identical to one with no
        // fundamentals until something says which it was.
        <Card className="mb-4 border-ink-700">
          <CardBody className="pt-5 text-sm text-zinc-400">
            Not in the securities table, so nothing was looked up:{" "}
            <span className="text-zinc-300">{data.meta.unknown_symbols.join(", ")}</span>
          </CardBody>
        </Card>
      )}

      {isLoading && !data ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : (
        <Card className="mb-4">
          <CardBody className="overflow-x-auto p-0">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-ink-700 text-left text-xs uppercase tracking-wider text-zinc-500">
                  <th className="px-4 py-2.5 font-medium">Symbol</th>
                  <th className="px-4 py-2.5 font-medium">Sector</th>
                  <th className="px-4 py-2.5 text-right font-medium">Price</th>
                  <th className="px-4 py-2.5 text-right font-medium">Mkt cap</th>
                  <th className="px-4 py-2.5 text-right font-medium">P/E</th>
                  <th className="px-4 py-2.5 text-right font-medium">Gross margin</th>
                  <th className="px-4 py-2.5 text-right font-medium">ROE</th>
                  <th className="px-4 py-2.5 text-right font-medium">F-score</th>
                  <th className="px-4 py-2.5 text-right font-medium">Periods</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const a = r.latest_annual;
                  const p = a?.piotroski_signals;
                  return (
                    <tr
                      key={r.symbol}
                      onClick={() => setSelected(r.symbol === selected ? null : r.symbol)}
                      className={cn(
                        "cursor-pointer border-b border-ink-800/60 hover:bg-ink-800/40",
                        r.symbol === selected && "bg-ink-800/60",
                      )}
                    >
                      <td className="px-4 py-2.5">
                        <div className="font-medium text-zinc-100">{r.symbol}</div>
                        <div className="text-xs text-zinc-500">{r.name ?? "—"}</div>
                      </td>
                      <td className="px-4 py-2.5 text-zinc-400">{r.sector ?? "—"}</td>
                      <td className="px-4 py-2.5 text-right tnum text-zinc-300">{fmt(r.market?.price ?? null, "usd")}</td>
                      <td className="px-4 py-2.5 text-right tnum text-zinc-300">{fmt(r.market?.market_cap ?? null, "usdCompact")}</td>
                      <td className="px-4 py-2.5 text-right tnum text-zinc-300">{fmt(r.market?.pe_trailing ?? null, "ratio")}</td>
                      <td className="px-4 py-2.5 text-right tnum text-zinc-300">{fmt(a?.gross_margin ?? null, "fraction")}</td>
                      <td className="px-4 py-2.5 text-right tnum text-zinc-300">{fmt(a?.roe ?? null, "fraction")}</td>
                      <td className={cn("px-4 py-2.5 text-right tnum", scoreTone(a?.piotroski_f_score ?? null))}>
                        {a?.piotroski_f_score ?? (p ? `${p.score}*` : "—")}
                      </td>
                      <td className="px-4 py-2.5 text-right tnum text-zinc-500">{r.periods_on_record}</td>
                    </tr>
                  );
                })}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={9} className="px-4 py-8 text-center text-sm text-zinc-500">
                      Nothing to show. The default view lists held names; search a symbol to widen it.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </CardBody>
        </Card>
      )}

      {active ? (
        <Detail row={active} />
      ) : (
        rows.length > 0 && (
          <p className="text-sm text-zinc-500">Select a row for the full set and its filed history.</p>
        )
      )}

      <p className="mt-6 text-xs text-zinc-600">
        Not available on this data plan, so they are absent rather than estimated:{" "}
        {KNOWN_ABSENT.join(", ")}.
      </p>
    </div>
  );
}
