"use client";

import { useMemo, useState } from "react";
import useSWR, { mutate } from "swr";
import { AlertTriangle, BookOpen, Check, Database, PenLine } from "lucide-react";
import { PageHeader } from "@/components/shell";
import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Spinner } from "@/components/ui";
import { fetcher, postJSON } from "@/lib/api";
import { ago, cn } from "@/lib/format";
import {
  ENTRY_META,
  ENTRY_TYPES,
  LIMITS,
  type EntryType,
  type JournalEntry,
  type JournalResponse,
} from "@/lib/journal";
import type { AccountView } from "@/lib/types";

const ENTRIES_PATH = "/api/history/entries?limit=200";

// ── the gap the charter actually cares about ──────────────────────────────────────────────────

/** Held names with no thesis on record.
 *
 *  This is the reason the page exists rather than a nicety. The charter treats a held position with
 *  no written reason as broken by definition, and reconciliation reports ten holdings nobody wrote
 *  down — but neither of those could be ACTED on, because there was no way to write the thesis.
 *
 *  Computed here from two payloads the page already loads rather than added as a backend endpoint:
 *  the join is a set difference over a handful of symbols, and doing it server-side would put a
 *  third consumer on the account read for no gain.
 */
function useThesisGap(account: AccountView | undefined, entries: JournalEntry[] | undefined) {
  return useMemo(() => {
    if (!account || !entries) return null;
    const withThesis = new Set(
      entries.filter((e) => e.entry_type === "thesis" && e.symbol).map((e) => e.symbol as string),
    );
    const held = account.positions.map((p) => p.symbol);
    return {
      missing: held.filter((s) => !withThesis.has(s)).sort(),
      covered: held.filter((s) => withThesis.has(s)).sort(),
    };
  }, [account, entries]);
}

// ── write forms ───────────────────────────────────────────────────────────────────────────────

function Field({
  label, value, onChange, placeholder, max, rows, required,
}: {
  label: string; value: string; onChange: (v: string) => void;
  placeholder?: string; max: number; rows?: number; required?: boolean;
}) {
  const over = value.length > max;
  const cls = cn(
    "w-full rounded-md border bg-ink-900 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none",
    over ? "border-loss focus:border-loss" : "border-ink-700 focus:border-zinc-500",
  );
  return (
    <label className="block">
      <span className="mb-1 flex items-baseline justify-between text-[11px] uppercase tracking-wider text-zinc-500">
        {label}{required && <span className="text-zinc-600">required</span>}
      </span>
      {rows ? (
        <textarea className={cls} rows={rows} value={value} placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input className={cls} value={value} placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)} />
      )}
      <span className={cn("mt-0.5 block text-right text-[11px]", over ? "text-loss" : "text-zinc-600")}>
        {value.length}/{max}
      </span>
    </label>
  );
}

function WriteThesis({ symbol, onDone }: { symbol?: string; onDone: () => void }) {
  const [sym, setSym] = useState(symbol ?? "");
  const [title, setTitle] = useState("");
  const [thesis, setThesis] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const ready = sym.trim() && title.trim() && thesis.trim()
    && title.length <= LIMITS.title && thesis.length <= LIMITS.thesis;

  async function submit() {
    setBusy(true); setErr(null);
    try {
      await postJSON("/api/history/thesis", {
        symbol: sym.trim().toUpperCase(), title: title.trim(), thesis: thesis.trim(),
      });
      setTitle(""); setThesis(""); if (!symbol) setSym("");
      await mutate(ENTRIES_PATH);
      onDone();
    } catch (e) {
      // Surfaced verbatim: the backend answers 409 with the exact domain reason (unknown symbol,
      // for one), and replacing that with "something went wrong" would discard the only text that
      // says what to do differently.
      setErr(e instanceof Error ? e.message : "Could not save the thesis");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <Field label="Symbol" value={sym} onChange={setSym} placeholder="TSM" max={10} required />
      <Field label="Title" value={title} onChange={setTitle} max={LIMITS.title} required
        placeholder="Why this position exists, in one line" />
      <Field label="Thesis" value={thesis} onChange={setThesis} max={LIMITS.thesis} rows={7} required
        placeholder="The case: what has to be true, what would break it, and what you would do then." />
      {err && (
        <p className="flex items-start gap-2 text-sm text-loss">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{err}
        </p>
      )}
      <Button variant="brass" onClick={submit} disabled={!ready || busy}>
        {busy ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : <Check className="h-4 w-4" />}
        {busy ? "Saving…" : "Record thesis"}
      </Button>
    </div>
  );
}

function WriteLesson({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [lesson, setLesson] = useState("");
  const [sym, setSym] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ready = title.trim() && lesson.trim() && title.length <= LIMITS.title && lesson.length <= LIMITS.lesson;

  async function submit() {
    setBusy(true); setErr(null);
    try {
      await postJSON("/api/history/lessons", {
        title: title.trim(), lesson: lesson.trim(),
        symbol: sym.trim() ? sym.trim().toUpperCase() : null,
      });
      setTitle(""); setLesson(""); setSym("");
      await mutate(ENTRIES_PATH);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save the lesson");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <Field label="Title" value={title} onChange={setTitle} max={LIMITS.title} required
        placeholder="What you now know that you did not" />
      <Field label="Lesson" value={lesson} onChange={setLesson} max={LIMITS.lesson} rows={6} required
        placeholder="The takeaway, stated so it changes a future decision." />
      <Field label="Symbol (optional)" value={sym} onChange={setSym} max={10}
        placeholder="Leave blank for a market-wide lesson" />
      {err && (
        <p className="flex items-start gap-2 text-sm text-loss">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{err}
        </p>
      )}
      <Button variant="brass" onClick={submit} disabled={!ready || busy}>
        {busy ? <Spinner className="border-ink-950/40 border-t-ink-950" /> : <Check className="h-4 w-4" />}
        {busy ? "Saving…" : "Record lesson"}
      </Button>
    </div>
  );
}

// ── page ──────────────────────────────────────────────────────────────────────────────────────

export default function JournalPage() {
  const { data, error, isLoading } = useSWR<JournalResponse>(ENTRIES_PATH, fetcher);
  const { data: account } = useSWR<AccountView>("/api/account", fetcher, { refreshInterval: 30_000 });
  const [filter, setFilter] = useState<EntryType | "all">("all");
  const [composing, setComposing] = useState<null | { kind: "thesis" | "lesson"; symbol?: string }>(null);

  const gap = useThesisGap(account, data?.entries);
  const entries = (data?.entries ?? []).filter((e) => filter === "all" || e.entry_type === filter);
  // An entry that has been corrected is still shown, but marked — the superseding entry names it.
  const superseded = new Set((data?.entries ?? []).map((e) => e.supersedes_id).filter(Boolean) as number[]);

  const header = (
    <PageHeader
      title="Journal"
      subtitle={data ? `${data.count} entries on record` : "Theses, outcomes and lessons"}
      right={
        <div className="flex items-center gap-2">
          <Button onClick={() => setComposing({ kind: "thesis" })}>
            <PenLine className="h-4 w-4" /> Thesis
          </Button>
          <Button onClick={() => setComposing({ kind: "lesson" })}>
            <BookOpen className="h-4 w-4" /> Lesson
          </Button>
        </div>
      }
    />
  );

  if (error) {
    const degraded = /\b503\b/.test(String(error.message ?? error));
    return (
      <div>
        {header}
        <Card className={cn(!degraded && "border-loss/40")}>
          <CardBody className="flex items-start gap-3 pt-5 text-sm text-zinc-400">
            <Database className="mt-0.5 h-4 w-4 shrink-0" />
            {degraded
              ? "The database is unavailable, so the journal cannot be read or written. The rest of the dashboard keeps working — this page is the only one that needs it."
              : String(error.message ?? error)}
          </CardBody>
        </Card>
      </div>
    );
  }

  return (
    <div>
      {header}

      {composing && (
        <Card className="mb-4 border-brass/40">
          <CardHeader>
            <CardTitle>{composing.kind === "thesis" ? "Record a thesis" : "Record a lesson"}</CardTitle>
            <button className="text-xs text-zinc-500 hover:text-zinc-300" onClick={() => setComposing(null)}>
              cancel
            </button>
          </CardHeader>
          <CardBody>
            {composing.kind === "thesis"
              ? <WriteThesis symbol={composing.symbol} onDone={() => setComposing(null)} />
              : <WriteLesson onDone={() => setComposing(null)} />}
          </CardBody>
        </Card>
      )}

      {gap && gap.missing.length > 0 && (
        <Card className="mb-4 border-loss/40">
          <CardHeader>
            <CardTitle>Held with no thesis on record</CardTitle>
            <span className="text-xs text-zinc-500">{gap.missing.length} of {gap.missing.length + gap.covered.length}</span>
          </CardHeader>
          <CardBody>
            <p className="mb-3 text-sm text-zinc-400">
              The charter treats a held position with no written reason as broken, whatever the P&amp;L
              says. These are positions the account holds and the record cannot explain.
            </p>
            <div className="flex flex-wrap gap-2">
              {gap.missing.map((s) => (
                <button
                  key={s}
                  onClick={() => setComposing({ kind: "thesis", symbol: s })}
                  className="rounded-md border border-ink-700 bg-ink-900 px-2.5 py-1 text-sm text-zinc-200 hover:border-zinc-500"
                >
                  {s} <span className="text-zinc-600">+ thesis</span>
                </button>
              ))}
            </div>
          </CardBody>
        </Card>
      )}

      {gap && gap.missing.length === 0 && gap.covered.length > 0 && (
        <Card className="mb-4">
          <CardBody className="flex items-center gap-2 pt-5 text-sm text-zinc-400">
            <Check className="h-4 w-4 text-gain" />
            Every held position has a thesis on record.
          </CardBody>
        </Card>
      )}

      <div className="mb-3 flex flex-wrap gap-1.5">
        {(["all", ...ENTRY_TYPES] as const).map((t) => (
          <button
            key={t}
            onClick={() => setFilter(t as EntryType | "all")}
            className={cn(
              "rounded-md px-2.5 py-1 text-xs",
              filter === t ? "bg-ink-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300",
            )}
          >
            {t === "all" ? "All" : ENTRY_META[t as EntryType].label}
          </button>
        ))}
      </div>

      {isLoading && !data ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : entries.length === 0 ? (
        <Card>
          <CardBody className="pt-5 text-sm text-zinc-500">
            {data?.count === 0
              ? "Nothing written down yet. A thesis here is what a future debate reads back as the case for a position."
              : "No entries of this type."}
          </CardBody>
        </Card>
      ) : (
        <div className="space-y-3">
          {entries.map((e) => {
            const meta = ENTRY_META[e.entry_type];
            return (
              <Card key={e.id} className={cn(superseded.has(e.id) && "opacity-60")}>
                <CardBody className="pt-5">
                  <div className="mb-1.5 flex flex-wrap items-center gap-2">
                    <Badge tone={meta.tone}>{meta.label}</Badge>
                    {e.symbol && <span className="text-sm font-medium text-zinc-100">{e.symbol}</span>}
                    <span className="text-xs text-zinc-600">{ago(e.as_of)}</span>
                    {superseded.has(e.id) && <Badge tone="hold">superseded</Badge>}
                    {e.supersedes_id && (
                      <span className="text-xs text-zinc-600">corrects #{e.supersedes_id}</span>
                    )}
                  </div>
                  <h3 className="text-sm font-medium text-zinc-100">{e.title}</h3>
                  {e.body && <p className="mt-1.5 whitespace-pre-wrap text-sm text-zinc-400">{e.body}</p>}
                  {e.lesson && (
                    <p className="mt-2 border-l-2 border-ink-700 pl-3 text-sm text-zinc-300">{e.lesson}</p>
                  )}
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}

      <p className="mt-6 text-xs text-zinc-600">
        Entries are append-only: a correction supersedes its predecessor rather than replacing it, so
        what was believed at the time survives. Exits are recorded against a specific open lot and
        live with the position, not here.
      </p>
    </div>
  );
}
