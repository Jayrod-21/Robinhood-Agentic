"use client";

// Per-position drill-down (reached from the Portfolio table). One name, everything the system knows
// about it in one place: the live holding, its slate target and role, the -20% stop and 1.3x trim
// math, the written thesis, an FMP price history, and the last debate that put it there. Read-only.
//
// Built against a dev fixture (NEXT_PUBLIC_POSITION_MOCK=1, see lib/position.ts) until the backend
// serves GET /api/position/{symbol} (docs/contracts/position-endpoint.md). The default (flag unset)
// hits the real endpoint and shows honest empty/degraded states, so a missing backend can never
// masquerade as real data. Price history routes through the backend so the FMP key stays server-side.

import Link from "next/link";
import useSWR from "swr";
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, ArrowLeft, Database, ShieldAlert } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Card, CardBody, CardHeader, CardTitle, Spinner, StatCard, decisionTone } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { useAccount, withAccount } from "@/components/account-context";
import { ago, cn, pct, plColor, usd } from "@/lib/format";
import { POSITION_MOCK, buildMockPosition, type PositionDetailResponse, type ThesisStatus } from "@/lib/position";

const THESIS_TONE: Record<ThesisStatus, string> = {
  intact: "text-gain",
  watch: "text-flat",
  broken: "text-loss",
};
const THESIS_LABEL: Record<ThesisStatus, string> = {
  intact: "thesis intact",
  watch: "on watch",
  broken: "thesis broken",
};

const fmtWeight = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)}%`);
const fmtDrift = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)} pts`);
const fmtDate = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

export default function PositionDetailPage({ params }: { params: { symbol: string } }) {
  const symbol = decodeURIComponent(params.symbol).toUpperCase();
  const { selectedId } = useAccount();
  const { data, error, isLoading } = useSWR<PositionDetailResponse>(
    POSITION_MOCK ? null : withAccount(`/api/position/${encodeURIComponent(symbol)}`, selectedId),
    fetcher,
    { refreshInterval: 15_000 },
  );
  const resp = POSITION_MOCK ? buildMockPosition(symbol) : data;

  const header = (
    <PositionHeader symbol={symbol} resp={resp} />
  );

  if (error) {
    const notFound = /\b404\b/.test(String(error.message ?? error));
    const degraded = /\b503\b/.test(String(error.message ?? error));
    return (
      <div>
        <BackLink />
        {header}
        <Card className={cn("border-loss/40", (degraded || notFound) && "border-ink-700")}>
          <CardBody className="flex items-start gap-3 pt-5 text-sm">
            {notFound ? (
              <>
                <Database className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" />
                <span className="text-zinc-400">
                  Nothing on record for {symbol}: it is not held, not in the slate, and has no debate
                  history.
                </span>
              </>
            ) : degraded ? (
              <>
                <Database className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" />
                <span className="text-zinc-400">
                  Position data for {symbol} isn&apos;t available right now. This view bundles the account
                  snapshot, slate, and price history; it fills in once those are readable.
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
        <BackLink />
        {header}
        <div className="flex items-center gap-2 px-1 py-8 text-sm text-zinc-500">
          <Spinner /> Loading {symbol}…
        </div>
      </div>
    );
  }

  const { meta, live, slate, stop, thesis, price_history, debate } = resp;
  const drifted = slate.drift_pct != null && Math.abs(slate.drift_pct) >= 5;

  return (
    <div>
      <BackLink />
      {header}

      {/* The loudest fact first: a broken thesis or a breached stop. */}
      {stop?.breached && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-loss/40 bg-loss/10 px-3 py-2 text-sm text-loss">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            <span className="font-medium">Past the -20% hard stop.</span>{" "}
            {symbol} is {pct(live?.unrealized_pl_pct)} against a -20% line. The slate says re-underwrite
            or exit, never average a broken thesis.
          </span>
        </div>
      )}
      {!slate.in_slate && meta.held && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-fuchsia-500/40 bg-fuchsia-500/10 px-3 py-2 text-sm text-fuchsia-300">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Held but not in the documented slate{!slate.in_universe && ", and off-universe"}. There is no
            thesis on record for {symbol}. It needs a written case or an exit.
          </span>
        </div>
      )}
      {!meta.held && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-flat/40 bg-flat/10 px-3 py-2 text-sm text-flat">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Documented in the slate at {fmtWeight(slate.target_weight_pct)} but not currently held. The
            live and stop cards below are blank because there is no position to measure.
          </span>
        </div>
      )}
      {meta.snapshot_stale && meta.held && (
        <div className="mb-3 flex items-start gap-2 text-xs text-zinc-500">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-600" />
          <span>
            Holding was last read from the broker {ago(meta.snapshot_generated_at)}, which is older
            than expected for a live read — treat the position size as unconfirmed.
          </span>
        </div>
      )}

      {/* Headline live numbers. */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Price" value={usd(live?.current_price)} sub={live ? `avg cost ${usd(live.average_buy_price)}` : "not held"} />
        <StatCard label="Market value" value={usd(live?.market_value)} sub={live ? `${live.quantity} sh` : "—"} />
        <StatCard
          label="Unrealized P&L"
          value={usd(live?.unrealized_pl)}
          sub={pct(live?.unrealized_pl_pct)}
          valueClass={plColor(live?.unrealized_pl)}
        />
        <StatCard
          label="Weight vs target"
          value={fmtWeight(live?.weight_account_pct)}
          sub={`target ${fmtWeight(slate.target_weight_pct)} · ${fmtDrift(slate.drift_pct)}`}
          valueClass={drifted ? "text-flat" : undefined}
        />
      </div>

      {/* Price history with the two lines that matter: what it cost you, and where the stop sits. */}
      <Card className="mt-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Price history</CardTitle>
          <span className="text-xs text-zinc-500">
            {meta.price_source}
            {meta.price_history_from && ` · since ${fmtDate(meta.price_history_from)}`}
          </span>
        </CardHeader>
        <CardBody>
          {price_history.length === 0 ? (
            <div className="flex items-center gap-2 py-8 text-sm text-zinc-500">
              <Database className="h-4 w-4" /> No price history available for {symbol}.
            </div>
          ) : (
            <PriceChart data={price_history} avgCost={live?.average_buy_price ?? null} />
          )}
        </CardBody>
      </Card>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_320px]">
        {/* The written case, then the last debate that argued it. */}
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex items-center justify-between">
              <CardTitle>Thesis</CardTitle>
              <span className="flex items-center gap-2">
                {slate.role && <span className="text-xs text-zinc-500">{slate.role}</span>}
                <Badge tone={thesis.status === "intact" ? "buy" : thesis.status === "watch" ? "hold" : "sell"}>
                  {THESIS_LABEL[thesis.status]}
                </Badge>
              </span>
            </CardHeader>
            <CardBody>
              {thesis.summary ? (
                <p className={cn("text-sm leading-relaxed", thesis.status === "broken" ? "text-loss" : "text-zinc-300")}>
                  {thesis.summary}
                </p>
              ) : (
                <p className="text-sm text-zinc-500">No thesis on record.</p>
              )}
              {slate.size_rationale && (
                <p className="mt-3 border-t border-ink-800 pt-3 text-xs leading-relaxed text-zinc-500">
                  <span className="uppercase tracking-wider text-zinc-600">Why this size · </span>
                  {slate.size_rationale}
                </p>
              )}
              {thesis.updated_at && (
                <p className="mt-2 text-[11px] text-zinc-600">Underwritten {ago(thesis.updated_at)}.</p>
              )}
            </CardBody>
          </Card>

          {debate && (
            <Card>
              <CardHeader className="flex items-center justify-between">
                <CardTitle>Last debate</CardTitle>
                <span className="flex items-center gap-2">
                  {debate.decision && <Badge tone={decisionTone(debate.decision)}>{debate.decision}</Badge>}
                  {debate.created_at && <span className="text-xs text-zinc-500">{ago(debate.created_at)}</span>}
                </span>
              </CardHeader>
              <CardBody>
                {debate.question && <p className="text-sm text-zinc-400">{debate.question}</p>}
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  {debate.bull_case && (
                    <div className="rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2">
                      <div className="text-xs font-medium uppercase tracking-wider text-gain">Bull</div>
                      <p className="mt-1 text-xs leading-relaxed text-zinc-400">{debate.bull_case}</p>
                    </div>
                  )}
                  {debate.bear_case && (
                    <div className="rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2">
                      <div className="text-xs font-medium uppercase tracking-wider text-loss">Bear</div>
                      <p className="mt-1 text-xs leading-relaxed text-zinc-400">{debate.bear_case}</p>
                    </div>
                  )}
                </div>
                <div className="mt-3 flex items-center justify-between">
                  {debate.jury_counts && debate.jury_total ? (
                    <span className="text-xs text-zinc-500">
                      Jury {debate.jury_total}: {debate.jury_counts.BUY ?? 0} buy · {debate.jury_counts.SELL ?? 0} sell ·{" "}
                      {debate.jury_counts.HOLD ?? 0} hold
                    </span>
                  ) : (
                    <span />
                  )}
                  <Link href={`/debate/${encodeURIComponent(debate.id)}`} className="text-xs text-brass transition-colors hover:text-brass/80">
                    Full debate →
                  </Link>
                </div>
              </CardBody>
            </Card>
          )}
        </div>

        {/* Discipline: the stop and the trim line, as numbers you act on. */}
        <Card>
          <CardHeader>
            <CardTitle>Discipline</CardTitle>
          </CardHeader>
          <CardBody className="space-y-3">
            {stop ? (
              <>
                <DisciplineRow
                  label="Hard stop"
                  value={`${stop.hard_stop_pct}%`}
                  detail={
                    stop.breached
                      ? "Breached. Re-underwrite or exit."
                      : stop.distance_to_stop_pct == null
                        ? "—"
                        : `${stop.distance_to_stop_pct.toFixed(1)} pts of cushion left.`
                  }
                  tone={stop.breached ? "text-loss" : "text-zinc-400"}
                />
                <DisciplineRow
                  label="Trim line"
                  value={fmtWeight(stop.trim_line_weight_pct)}
                  detail={
                    stop.trim_line_weight_pct == null
                      ? "No target to size against."
                      : stop.above_trim_line
                        ? `Above 1.3x target (${fmtWeight(live?.weight_account_pct)}). Trim the winner.`
                        : `At ${fmtWeight(live?.weight_account_pct)}, under the 1.3x-target trim line.`
                  }
                  tone={stop.above_trim_line ? "text-flat" : "text-zinc-400"}
                />
              </>
            ) : (
              <p className="text-sm text-zinc-500">No live position, so there is nothing to stop out or trim.</p>
            )}
            <div className="border-t border-ink-800 pt-3 text-xs text-zinc-500">
              <div className="flex justify-between">
                <span>Universe</span>
                <span className={slate.in_universe ? "text-zinc-400" : "text-fuchsia-400"}>
                  {slate.in_universe ? "in universe" : "off-universe"}
                </span>
              </div>
              <div className="mt-1 flex justify-between">
                <span>Slate</span>
                <span className={slate.in_slate ? "text-zinc-400" : "text-fuchsia-400"}>
                  {slate.in_slate ? "documented" : "unrecorded"}
                </span>
              </div>
            </div>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function PositionHeader({ symbol, resp }: { symbol: string; resp: PositionDetailResponse | undefined }) {
  return (
    <PageHeader
      title={symbol}
      subtitle={
        resp
          ? [resp.meta.name, resp.meta.sector].filter(Boolean).join(" · ") || "Position detail"
          : "Position detail"
      }
      right={POSITION_MOCK ? <Badge tone="hold">MOCK DATA</Badge> : undefined}
    />
  );
}

function DisciplineRow({ label, value, detail, tone }: { label: string; value: string; detail: string; tone: string }) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-sm text-zinc-300">{label}</span>
        <span className={cn("tnum text-sm font-medium", tone)}>{value}</span>
      </div>
      <p className="mt-0.5 text-xs text-zinc-500">{detail}</p>
    </div>
  );
}

function PriceChart({ data, avgCost }: { data: { date: string; close: number }[]; avgCost: number | null }) {
  const closes = data.map((d) => d.close);
  const lo = Math.min(...closes, avgCost ?? Infinity);
  const hi = Math.max(...closes, avgCost ?? -Infinity);
  const pad = (hi - lo) * 0.08 || hi * 0.05;
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#e0b34d" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#e0b34d" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="date"
            tick={{ fill: "#71717a", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "#2e323b" }}
            minTickGap={40}
            tickFormatter={fmtDate}
          />
          <YAxis
            domain={[lo - pad, hi + pad]}
            tick={{ fill: "#71717a", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={48}
            tickFormatter={(v: number) => `$${v.toFixed(0)}`}
          />
          {avgCost != null && (
            <ReferenceLine
              y={avgCost}
              stroke="#71717a"
              strokeDasharray="4 4"
              label={{ value: `avg ${usd(avgCost)}`, fill: "#a1a1aa", fontSize: 10, position: "insideTopRight" }}
            />
          )}
          <Tooltip
            contentStyle={{ background: "#181a1f", border: "1px solid #2e323b", borderRadius: 8, fontSize: 12 }}
            labelFormatter={(l) => fmtDate(String(l))}
            formatter={(v: number) => [usd(v), "close"]}
          />
          <Area type="monotone" dataKey="close" stroke="#e0b34d" strokeWidth={1.5} fill="url(#priceFill)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/" className="mb-4 inline-flex items-center gap-1.5 text-sm text-zinc-500 transition-colors hover:text-zinc-200">
      <ArrowLeft className="h-4 w-4" /> Portfolio
    </Link>
  );
}
