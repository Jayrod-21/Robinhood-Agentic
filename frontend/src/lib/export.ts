// Client-side export helpers. The debate detail page already fetches the full record from
// /api/debate/{id}, so exporting it is a pure browser operation (Blob + object URL), no backend
// endpoint needed. This keeps the transcript export working today; a bulk server-side export
// (docs/contracts covers it) can come later for whole-corpus dumps.

import type { DebateDetail, JurorVote } from "@/lib/types";

/** Trigger a browser download of `text` as `filename`. Revokes the object URL after the click. */
export function downloadText(filename: string, text: string, mime = "text/plain"): void {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function slug(s: string): string {
  return s.replace(/[^a-zA-Z0-9-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
}

/** A stable, readable base name for a debate export, e.g. "debate-nvda-2026-08-14". */
export function debateFilename(d: DebateDetail): string {
  const who = d.ticker ? slug(d.ticker) : slug(d.id);
  const day = d.created_at ? d.created_at.slice(0, 10) : "undated";
  return `debate-${who}-${day}`;
}

function voteLine(v: JurorVote): string {
  const conf = `${Math.round(v.confidence * 100)}%`;
  return `- **#${v.agent_id} ${v.focus_area.replace(/_/g, " ")}** ${v.vote} (conf ${conf}): ${v.reasoning}`;
}

/** Build a readable markdown transcript from a debate record. Archived records already carry their
 *  own `markdown`; engine records are reconstructed from the structured fields so both export the same. */
export function debateToMarkdown(d: DebateDetail): string {
  if (d.markdown) return d.markdown;

  const lines: string[] = [];
  lines.push(`# ${d.ticker ? `${d.ticker}: Jury Debate` : "Archived Debate"}`);
  if (d.question) lines.push(`\n> ${d.question}`);
  const meta: string[] = [];
  if (d.final_decision) meta.push(`Decision: **${d.final_decision}**`);
  if (d.price != null) meta.push(`Price at debate: $${d.price.toFixed(2)}`);
  if (d.created_at) meta.push(d.created_at);
  if (d.source) meta.push(`source: ${d.source}`);
  if (meta.length) lines.push(`\n${meta.join(" · ")}`);

  if (d.bull_bear?.bull_case) lines.push(`\n## Bull case\n\n${d.bull_bear.bull_case}`);
  if (d.bull_bear?.bear_case) lines.push(`\n## Bear case\n\n${d.bull_bear.bear_case}`);

  if (d.jury) {
    lines.push(`\n## Jury (${d.jury.votes.length} votes)`);
    if (d.jury.counts) {
      lines.push(`\nTally: ${Object.entries(d.jury.counts).map(([k, v]) => `${v} ${k}`).join(" · ")}`);
    }
    if (d.jury.escalated_to_human) lines.push(`\n**Escalated to human** (${d.jury.reason})`);
    lines.push("");
    for (const v of d.jury.votes) lines.push(voteLine(v));
    if (d.jury.decision) lines.push(`\n**Jury decision: ${d.jury.decision}.** ${d.jury.reason}`);
  }

  if (d.position_size_note) lines.push(`\n## Position sizing\n\n${d.position_size_note}`);
  if (d.models && Object.keys(d.models).length) {
    lines.push(`\n---\nModels: ${Object.entries(d.models).map(([k, m]) => `${k} ${m}`).join(" · ")}`);
  }
  return lines.join("\n") + "\n";
}
