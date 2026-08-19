"use client";

// The Claude chat drawer: a toggle on every page opening a right-side panel where the operator asks
// Claude about the book, and Claude can propose a tunable-weight change the operator confirms. v1:
// analyze + propose settings, each confirmed. NO trades (see lib/chat.ts). Reuses the debate markdown
// renderer for responses; the live streaming path (POST /api/chat via streamSSE) is wired when the
// backend lands. Built mock-first (NEXT_PUBLIC_CHAT_MOCK=1); without it, the drawer shows an honest
// "not connected yet" state rather than pretending to think.

import { useEffect, useRef, useState } from "react";
import { Check, MessageSquare, Send, Sparkles, X } from "lucide-react";
import { Markdown } from "@/components/markdown";
import { cn } from "@/lib/format";
import { CHAT_MOCK, CHAT_MODEL_LABEL, MOCK_THREAD, type ChatMessage, type SettingProposal } from "@/lib/chat";

export function ChatDrawer() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>(() => (CHAT_MOCK ? MOCK_THREAD : []));
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  function send() {
    const text = input.trim();
    if (!text || busy) return;
    setMessages((m) => [...m, { id: `u${Date.now()}`, role: "user", content: text }]);
    setInput("");
    if (!CHAT_MOCK) return; // live: POST /api/chat and stream the reply (backend pending)
    setBusy(true);
    window.setTimeout(() => {
      setMessages((m) => [
        ...m,
        {
          id: `a${Date.now()}`,
          role: "assistant",
          content:
            "*(mock)* Once the chat backend is connected I'd pull the live portfolio, debates, and settings to answer that. This drawer shows the shape: analysis, plus weight changes I propose and you confirm.",
        },
      ]);
      setBusy(false);
    }, 400);
  }

  function resolveProposal(msgId: string, status: "applied" | "dismissed") {
    const msg = messages.find((x) => x.id === msgId);
    setMessages((m) =>
      m.map((x) => (x.id === msgId && x.proposal ? { ...x, proposal: { ...x.proposal, status } } : x)),
    );
    // live: on "applied", PUT /api/settings/{key} then report the result. In mock, report locally.
    if (status === "applied" && msg?.proposal) {
      const p = msg.proposal;
      setMessages((m) => [
        ...m,
        {
          id: `s${Date.now()}`,
          role: "assistant",
          content: `Done. **${p.label}** is now **${p.proposed}${p.unit ? ` ${p.unit}` : ""}** (was ${p.current}). Logged to the settings history.`,
        },
      ]);
    }
  }

  return (
    <>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-label="Ask Claude"
        aria-expanded={open}
        className="fixed bottom-5 right-5 z-40 inline-flex h-12 w-12 items-center justify-center rounded-full border border-brass/40 bg-brass/90 text-ink-950 shadow-lg transition-colors hover:bg-brass"
      >
        {open ? <X className="h-5 w-5" /> : <MessageSquare className="h-5 w-5" />}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Ask Claude"
          className="fixed bottom-0 right-0 top-0 z-40 flex w-full flex-col border-l border-ink-800 bg-ink-950/95 backdrop-blur sm:w-[26rem]"
        >
          <div className="flex items-center justify-between border-b border-ink-800 px-4 py-3">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-brass" />
              <span className="font-serif text-lg text-zinc-100">Ask Claude</span>
              <span className="rounded bg-ink-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-400">{CHAT_MODEL_LABEL}</span>
            </div>
            <button onClick={() => setOpen(false)} aria-label="Close" className="text-zinc-500 hover:text-zinc-200">
              <X className="h-5 w-5" />
            </button>
          </div>

          <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
            {messages.length === 0 && (
              <div className="mt-8 text-center text-sm text-zinc-500">
                {CHAT_MOCK
                  ? "Ask about the book."
                  : "The chat backend isn't connected yet. When it is, ask Claude about debates, models, and the portfolio here."}
              </div>
            )}
            {messages.map((m) => (
              <MessageRow key={m.id} m={m} onConfirm={() => resolveProposal(m.id, "applied")} onDismiss={() => resolveProposal(m.id, "dismissed")} />
            ))}
            {busy && (
              <div className="flex items-center gap-2 text-xs text-zinc-500">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brass" /> thinking…
              </div>
            )}
          </div>

          <div className="border-t border-ink-800 px-3 py-3">
            <div className="flex items-end gap-2">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                rows={1}
                placeholder={CHAT_MOCK ? "Ask a question…" : "Chat backend pending…"}
                disabled={!CHAT_MOCK}
                className="max-h-32 flex-1 resize-none rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-brass/50 focus:outline-none disabled:opacity-50"
              />
              <button
                onClick={send}
                disabled={!CHAT_MOCK || !input.trim() || busy}
                aria-label="Send"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brass/90 text-ink-950 transition-colors hover:bg-brass disabled:opacity-40"
              >
                <Send className="h-4 w-4" />
              </button>
            </div>
            <p className="mt-2 px-1 text-[11px] leading-snug text-zinc-600">
              Claude can analyze and propose weight changes you confirm. It cannot place trades.
            </p>
          </div>
        </div>
      )}
    </>
  );
}

function MessageRow({ m, onConfirm, onDismiss }: { m: ChatMessage; onConfirm: () => void; onDismiss: () => void }) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-lg rounded-br-sm bg-ink-800 px-3 py-2 text-sm text-zinc-100">{m.content}</div>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div className="max-w-[92%] text-sm text-zinc-300">
        <Markdown text={m.content} />
      </div>
      {m.proposal && <ProposalCard p={m.proposal} onConfirm={onConfirm} onDismiss={onDismiss} />}
    </div>
  );
}

function ProposalCard({ p, onConfirm, onDismiss }: { p: SettingProposal; onConfirm: () => void; onDismiss: () => void }) {
  const unit = p.unit ? ` ${p.unit}` : "";
  return (
    <div className="rounded-lg border border-brass/30 bg-brass/5 px-3 py-2.5">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-brass">
        <Sparkles className="h-3.5 w-3.5" /> Proposed change
      </div>
      <div className="mt-1.5 flex items-baseline gap-2 text-sm">
        <span className="text-zinc-300">{p.label}</span>
        <span className="tnum text-zinc-500">{p.current}{unit}</span>
        <span className="text-zinc-600">→</span>
        <span className="tnum font-medium text-zinc-100">{p.proposed}{unit}</span>
      </div>
      <p className="mt-1 text-xs leading-relaxed text-zinc-500">{p.rationale}</p>
      {p.status === "pending" ? (
        <div className="mt-2.5 flex gap-2">
          <button onClick={onConfirm} className="inline-flex items-center gap-1.5 rounded-md bg-brass/90 px-3 py-1.5 text-xs font-semibold text-ink-950 transition-colors hover:bg-brass">
            <Check className="h-3.5 w-3.5" /> Confirm
          </button>
          <button onClick={onDismiss} className="rounded-md px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:text-zinc-100">
            Dismiss
          </button>
        </div>
      ) : (
        <div className={cn("mt-2 text-xs font-medium", p.status === "applied" ? "text-gain" : "text-zinc-500")}>
          {p.status === "applied" ? "Confirmed and applied" : "Dismissed"}
        </div>
      )}
    </div>
  );
}
