"use client";

// Export the current debate record. The page already has the full record client-side, so these are
// pure browser downloads (no backend): JSON for the machine-readable record, Markdown for the
// readable transcript.

import { Download } from "lucide-react";
import { debateFilename, debateToMarkdown, downloadText } from "@/lib/export";
import type { DebateDetail } from "@/lib/types";

export function DebateExport({ data }: { data: DebateDetail }) {
  const base = debateFilename(data);
  return (
    <div className="flex items-center gap-1">
      <button
        onClick={() => downloadText(`${base}.json`, JSON.stringify(data, null, 2), "application/json")}
        title="Export as JSON"
        className="inline-flex items-center gap-1 rounded-md border border-ink-700 px-2 py-1 text-xs text-zinc-400 transition-colors hover:bg-ink-800 hover:text-zinc-100"
      >
        <Download className="h-3.5 w-3.5" /> JSON
      </button>
      <button
        onClick={() => downloadText(`${base}.md`, debateToMarkdown(data), "text/markdown")}
        title="Export as Markdown"
        className="inline-flex items-center gap-1 rounded-md border border-ink-700 px-2 py-1 text-xs text-zinc-400 transition-colors hover:bg-ink-800 hover:text-zinc-100"
      >
        <Download className="h-3.5 w-3.5" /> MD
      </button>
    </div>
  );
}
