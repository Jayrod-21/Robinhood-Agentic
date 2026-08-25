"use client";

// The Data-Trust strip: a thin, always-visible bar at the top of every page (rendered by the Shell)
// that states how much to trust what you are seeing. Snapshot freshness, price source and coverage,
// the price-only caveat, and the two posture facts (auth enforced, live debates) that change what
// the numbers mean. If any page is showing a fixture, it says so, loudly, on the right.
//
// Read-only. Built against a dev fixture (NEXT_PUBLIC_TRUST_MOCK=1, see lib/dataTrust.ts) until the
// backend serves GET /api/data-trust. The point of this strip is honesty, so it fails honest: when
// it cannot reach the endpoint it says "data status unavailable" rather than showing a reassuring
// green it hasn't earned.

import useSWR from "swr";
import { Clock, Database, FlaskConical, ShieldAlert, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui";
import { fetcher } from "@/lib/api";
import { useAccount, withAccount } from "@/components/account-context";
import { ago, cn } from "@/lib/format";
import { ANY_MOCK, MOCK_DATA_TRUST, TRUST_MOCK, type DataTrustResponse } from "@/lib/dataTrust";

type Tone = "ok" | "warn" | "muted";
const TONE_TEXT: Record<Tone, string> = { ok: "text-gain", warn: "text-flat", muted: "text-zinc-500" };
const TONE_DOT: Record<Tone, string> = { ok: "bg-gain", warn: "bg-flat", muted: "bg-zinc-600" };

function Chip({ tone, dot, icon: Icon, children }: { tone: Tone; dot?: boolean; icon?: typeof Clock; children: React.ReactNode }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", TONE_TEXT[tone])}>
      {dot && <span className={cn("h-1.5 w-1.5 rounded-full", TONE_DOT[tone])} />}
      {Icon && <Icon className="h-3.5 w-3.5" />}
      {children}
    </span>
  );
}

function Bar({ children }: { children: React.ReactNode }) {
  return (
    <div className="sticky top-0 z-20 -mx-4 -mt-6 mb-6 border-b border-ink-800 bg-ink-950/85 px-4 py-2 backdrop-blur sm:-mx-8 sm:px-8">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">{children}</div>
    </div>
  );
}

export function DataTrustStrip() {
  const { selectedId } = useAccount();
  const { data, error, isLoading } = useSWR<DataTrustResponse>(
    TRUST_MOCK ? null : withAccount("/api/data-trust", selectedId),
    fetcher,
    { refreshInterval: 30_000, shouldRetryOnError: false },
  );
  const trust = TRUST_MOCK ? MOCK_DATA_TRUST : data;

  // Fail honest: no reassuring green we haven't earned. If any page is mocked, still say so.
  if (error || (!trust && !isLoading)) {
    return (
      <Bar>
        <Chip tone="muted" icon={Database}>
          Data status unavailable
        </Chip>
        {ANY_MOCK && <MockChip />}
      </Bar>
    );
  }

  if (!trust) {
    return (
      <Bar>
        <Chip tone="muted" dot>
          Checking data status…
        </Chip>
        {ANY_MOCK && <MockChip />}
      </Bar>
    );
  }

  const freshTone: Tone = trust.snapshot_stale ? "warn" : "ok";
  const fullCoverage = trust.positions_priced >= trust.positions_total && !trust.prices_degraded;
  const covTone: Tone = fullCoverage ? "ok" : "warn";

  return (
    <Bar>
      <Chip tone={freshTone} icon={Clock}>
        Account read {ago(trust.snapshot_generated_at)}
        {trust.snapshot_stale && <span className="ml-1 text-zinc-500">(stale)</span>}
      </Chip>

      <Chip tone={covTone} icon={Database}>
        {trust.price_source} · {trust.positions_priced}/{trust.positions_total} priced
        {trust.prices_degraded && <span className="ml-1 text-zinc-500">(gaps)</span>}
      </Chip>

      {trust.returns_basis === "price_only" && (
        <Chip tone="muted">price-only returns</Chip>
      )}

      {trust.auth_enforced ? (
        <Chip tone="ok" icon={ShieldCheck}>auth enforced</Chip>
      ) : (
        <Chip tone="warn" icon={ShieldAlert}>auth off</Chip>
      )}

      <Chip tone="muted" dot>
        {trust.debate_live ? "debates live" : "debates: history-only"}
      </Chip>

      {ANY_MOCK && (
        <span className="ml-auto">
          <MockChip />
        </span>
      )}
    </Bar>
  );
}

function MockChip() {
  return (
    <Badge tone="hold" className="gap-1">
      <FlaskConical className="h-3 w-3" />
      MOCK DATA
    </Badge>
  );
}
