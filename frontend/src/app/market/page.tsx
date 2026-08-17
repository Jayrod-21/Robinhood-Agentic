"use client";

// Market page: read-only market context from the Market Mover daily brief. The catalyst calendar
// (flagged against held/slate names, with the rental window called out) sits up top because that is
// what the slate acts on; the headline feed for names in the book sits below. A one-line macro read
// leads. No order path, no per-name price calls, no secrets.
//
// Built against a dev fixture (NEXT_PUBLIC_MARKET_MOCK=1, see lib/market.ts) until the backend serves
// GET /api/market-context. The default (flag unset) hits the real endpoint and shows honest
// empty/degraded states, so a missing backend can never masquerade as real news.

import useSWR from "swr";
import Link from "next/link";
import { AlertTriangle, CalendarClock, Database, Newspaper } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { ago, cn } from "@/lib/format";
import { MARKET_MOCK, MOCK_MARKET_CONTEXT, type Catalyst, type Headline, type MarketContextResponse, type Sentiment } from "@/lib/market";

const SENTIMENT_DOT: Record<Sentiment, string> = { positive: "bg-gain", negative: "bg-loss", neutral: "bg-zinc-600" };

const daysLabel = (n: number | null) => (n == null ? "" : n <= 0 ? "today" : n === 1 ? "1 day" : `${n} days`);
const catalystDate = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

export default function MarketPage() {
  const { data, error, isLoading } = useSWR<MarketContextResponse>(
    MARKET_MOCK ? null : "/api/market-context",
    fetcher,
    { refreshInterval: 60_000 },
  );
  const resp = MARKET_MOCK ? MOCK_MARKET_CONTEXT : data;

  const header = (
    <PageHeader
      title="Market"
      subtitle={
        resp
          ? `${resp.meta.source} brief${resp.meta.brief_generated_at ? `, ${ago(resp.meta.brief_generated_at)}` : ""}`
          : "Market Mover context for the book"
      }
      right={MARKET_MOCK ? <Badge tone="hold">MOCK DATA</Badge> : undefined}
    />
  );

  if (error) {
    const degraded = /\b(404|503)\b/.test(String(error.message ?? error));
    return (
      <div>
        {header}
        <Card className={cn("border-loss/40", degraded && "border-ink-700")}>
          <CardBody className="flex items-start gap-3 pt-5 text-sm">
            {degraded ? (
              <>
                <Database className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" />
                <span className="text-zinc-400">
                  No market brief is available right now. This page reads the Market Mover daily brief;
                  it fills in once the brief is ingested.
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
          <Spinner /> Loading the market brief…
        </div>
      </div>
    );
  }

  const { meta, catalysts, headlines } = resp;
  const sortedCatalysts = [...catalysts].sort((a, b) => (a.days_until ?? 1e9) - (b.days_until ?? 1e9));

  return (
    <div>
      {header}

      {meta.brief_stale && (
        <div className="mb-3 flex items-start gap-2 text-xs text-flat">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            Reading a brief from {ago(meta.brief_generated_at)}. The market has moved since; treat catalysts
            and headlines below as of that time.
          </span>
        </div>
      )}

      {meta.macro_read && (
        <Card className="mb-4 border-brass/20 bg-brass/5">
          <CardBody className="pt-5 text-sm leading-relaxed text-zinc-300">{meta.macro_read}</CardBody>
        </Card>
      )}

      {/* Catalysts first: this is the part the slate acts on. */}
      <Card>
        <CardHeader className="flex items-center gap-2">
          <CalendarClock className="h-4 w-4 text-zinc-500" />
          <CardTitle>Catalyst calendar</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {sortedCatalysts.length === 0 ? (
            <p className="px-5 py-6 text-sm text-zinc-500">No dated catalysts in the brief.</p>
          ) : (
            <ul className="divide-y divide-ink-850">
              {sortedCatalysts.map((c, i) => (
                <CatalystRow key={`${c.symbol ?? "macro"}-${c.label}-${i}`} c={c} />
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      {/* Then the headline feed for names in the book. */}
      <Card className="mt-4">
        <CardHeader className="flex items-center gap-2">
          <Newspaper className="h-4 w-4 text-zinc-500" />
          <CardTitle>Headlines</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {headlines.length === 0 ? (
            <p className="px-5 py-6 text-sm text-zinc-500">No headlines for names in the book.</p>
          ) : (
            <ul className="divide-y divide-ink-850">
              {headlines.map((h) => (
                <HeadlineRow key={h.id} h={h} />
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      <p className="mt-4 text-[11px] leading-relaxed text-zinc-600">
        Read-only context from the Market Mover brief. Headlines are the brief&apos;s record, not a live wire;
        act on catalysts against the documented slate, not on a single headline.
      </p>
    </div>
  );
}

function CatalystRow({ c }: { c: Catalyst }) {
  const soon = c.days_until != null && c.days_until <= 3;
  return (
    <li className="flex items-start gap-3 px-5 py-3">
      <div className="w-16 shrink-0 pt-0.5">
        <div className={cn("text-sm font-medium tnum", soon ? "text-flat" : "text-zinc-300")}>{daysLabel(c.days_until)}</div>
        <div className="text-[11px] text-zinc-600">{catalystDate(c.date)}</div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          {c.symbol ? (
            <Link href={`/position/${encodeURIComponent(c.symbol)}`} className="text-sm font-medium text-zinc-100 transition-colors hover:text-brass">
              {c.symbol}
            </Link>
          ) : (
            <span className="text-sm font-medium text-zinc-300">Macro</span>
          )}
          <span className="text-sm text-zinc-400">{c.label}</span>
          {c.rental_window && <Badge tone="escalated">rental window</Badge>}
          {c.symbol && !c.held && c.in_slate && <Badge tone="sell">not held</Badge>}
          {c.symbol && c.held && <Badge tone="buy">held</Badge>}
          {c.symbol && !c.in_slate && <span className="text-[10px] uppercase tracking-wide text-fuchsia-400">off-slate</span>}
        </div>
        {c.note && <p className="mt-1 text-xs leading-relaxed text-zinc-500">{c.note}</p>}
      </div>
    </li>
  );
}

function HeadlineRow({ h }: { h: Headline }) {
  const inner = (
    <div className="flex items-start gap-3">
      {h.sentiment && <span className={cn("mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full", SENTIMENT_DOT[h.sentiment])} />}
      <div className="min-w-0 flex-1">
        <div className="text-sm text-zinc-100 group-hover:text-brass">{h.title}</div>
        {h.summary && <p className="mt-0.5 text-xs leading-relaxed text-zinc-500">{h.summary}</p>}
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-zinc-600">
          <span>{h.source}</span>
          <span>·</span>
          <span>{ago(h.published_at)}</span>
          {h.tickers.map((t) => (
            <span key={t} className="rounded bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-zinc-400">
              {t}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
  return (
    <li className="px-5 py-3">
      {h.url ? (
        <a href={h.url} target="_blank" rel="noopener noreferrer" className="group block">
          {inner}
        </a>
      ) : (
        <div className="group">{inner}</div>
      )}
    </li>
  );
}
