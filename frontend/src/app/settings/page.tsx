"use client";

import { useState } from "react";
import useSWR from "swr";
import { AlertTriangle, Database, FileText, RotateCcw } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner } from "@/components/ui";
import { API_URL, CREDENTIALS, fetcher } from "@/lib/api";
import { ago, cn } from "@/lib/format";
import type { Parameter, SettingsResponse } from "@/lib/settings";

const PATH = "/api/settings";

async function putSetting(key: string, value: number) {
  const res = await fetch(`${API_URL}/api/settings/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
    credentials: CREDENTIALS,
  });
  if (!res.ok) {
    // The API answers 422 with the registry's own sentence naming the bound. Showing that verbatim
    // is the difference between "invalid value" and "must be between 0.1 and 25 pp".
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep the status */
    }
    throw new Error(detail);
  }
  return res.json();
}

function Row({ p, onSaved }: { p: Parameter; onSaved: () => void }) {
  const [draft, setDraft] = useState(String(p.value));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  const parsed = Number(draft);
  const changed = draft.trim() !== "" && Number.isFinite(parsed) && parsed !== p.value;
  const outOfRange = Number.isFinite(parsed) && (parsed < p.min || parsed > p.max);

  async function save(next: number) {
    setBusy(true); setErr(null); setOk(false);
    try {
      await putSetting(p.key, next);
      setDraft(String(next));
      setOk(true);
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-b border-ink-800/60 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-[190px] flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm text-zinc-200">{p.label}</span>
            {!p.is_default && <Badge tone="hold">changed</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-zinc-500">{p.help}</p>
          <p className="mt-0.5 text-[11px] text-zinc-600">{p.used_by}</p>
        </div>

        <div className="flex items-center gap-1.5">
          <input
            value={draft}
            onChange={(e) => { setDraft(e.target.value); setOk(false); setErr(null); }}
            inputMode="decimal"
            className={cn(
              "w-24 rounded-md border bg-ink-900 px-2 py-1.5 text-right text-sm tnum text-zinc-200 focus:outline-none",
              outOfRange ? "border-loss" : "border-ink-700 focus:border-zinc-500",
            )}
          />
          <span className="w-8 text-xs text-zinc-500">{p.unit}</span>
          <Button variant="brass" onClick={() => save(parsed)} disabled={!changed || outOfRange || busy}>
            {busy ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : null}
            Save
          </Button>
          <button
            title={`Reset to ${p.default}`}
            onClick={() => save(p.default)}
            disabled={busy || p.is_default}
            className="rounded-md p-1.5 text-zinc-600 hover:text-zinc-300 disabled:opacity-30"
          >
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="mt-1 flex flex-wrap items-center gap-x-3 text-[11px]">
        <span className="text-zinc-600">
          allowed {p.min}–{p.max} {p.unit} · default {p.default}
        </span>
        {ok && <span className="text-gain">saved</span>}
        {err && <span className="text-loss">{err}</span>}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const { data, error, isLoading, mutate } = useSWR<SettingsResponse>(PATH, fetcher);

  const header = <PageHeader title="Parameters" subtitle="Thresholds the checks and the screen run on" />;

  if (error) {
    return (
      <div>
        {header}
        <Card className="border-loss/40">
          <CardBody className="pt-5 text-sm text-zinc-400">{String(error.message ?? error)}</CardBody>
        </Card>
      </div>
    );
  }
  if (isLoading && !data) {
    return <div>{header}<div className="flex justify-center py-12"><Spinner /></div></div>;
  }
  if (!data) return <div>{header}</div>;

  const groups = Array.from(new Set(data.parameters.map((p) => p.group)));

  return (
    <div>
      {header}

      {data.meta.source === "defaults" && (
        <Card className="mb-4 border-loss/40">
          <CardBody className="flex items-start gap-3 pt-5 text-sm text-zinc-300">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-loss" />
            <span>
              The database could not be read, so every value below is the built-in default — not a
              value anyone chose. The checks are running on these defaults right now, and changes
              cannot be saved until the database is back.
            </span>
          </CardBody>
        </Card>
      )}

      {groups.map((g) => (
        <Card key={g} className="mb-4">
          <CardHeader><CardTitle>{g}</CardTitle></CardHeader>
          <CardBody className="py-0">
            {data.parameters.filter((p) => p.group === g).map((p) => (
              <Row key={p.key} p={p} onSaved={() => mutate()} />
            ))}
          </CardBody>
        </Card>
      ))}

      <Card className="mb-4">
        <CardHeader>
          <CardTitle>Set in the document, not here</CardTitle>
          <span className="text-xs text-zinc-500">docs/SLATE.md</span>
        </CardHeader>
        <CardBody className="space-y-1.5 text-sm">
          {data.meta.document_sourced.map((d) => (
            <div key={d.label} className="flex flex-wrap items-baseline gap-2">
              <FileText className="h-3.5 w-3.5 shrink-0 text-zinc-600" />
              <span className="text-zinc-300">{d.label}</span>
              <span className="text-zinc-500">{d.value_note}</span>
            </div>
          ))}
          <p className="pt-1 text-xs text-zinc-600">
            These stay in the document on purpose: the plan an owner edits is meant to outrank the
            dashboard. Moving them here would let this page quietly win instead.
          </p>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Changes</CardTitle>
          <span className="text-xs text-zinc-500">newest first</span>
        </CardHeader>
        <CardBody>
          {data.history.length === 0 ? (
            <p className="flex items-center gap-2 text-sm text-zinc-500">
              <Database className="h-4 w-4" /> Nothing has been changed from its default yet.
            </p>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {data.history.map((h, i) => (
                <li key={i} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-zinc-300">{h.label}</span>
                  <span className="tnum text-zinc-500">
                    {h.old_value ?? "default"} → {h.new_value}
                  </span>
                  <span className="text-xs text-zinc-600">
                    {h.changed_by ?? "unknown"} · {ago(h.changed_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
