// Minimal markdown renderer for debate records (archive narratives + bull/bear cases).
//
// Deliberately dependency-free and XSS-safe by construction: it never touches
// dangerouslySetInnerHTML — every construct becomes a React element, so record content (which
// includes LLM output) is always rendered as text, never interpreted as HTML. Covers the subset
// the debate logs actually use: headings, tables, lists, blockquotes, hr, bold/italic/code/links.

import * as React from "react";

const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)|(\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;

function renderInline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push(text.slice(last, idx));
    const tok = m[0];
    if (tok.startsWith("`")) {
      out.push(
        <code key={key++} className="rounded bg-ink-800 px-1 py-0.5 font-mono text-[0.85em] text-brass">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      out.push(
        <strong key={key++} className="font-semibold text-zinc-100">
          {renderInline(tok.slice(2, -2))}
        </strong>,
      );
    } else if (tok.startsWith("*") || tok.startsWith("_")) {
      out.push(<em key={key++}>{renderInline(tok.slice(1, -1))}</em>);
    } else {
      // [label](https://url) — only http(s) URLs match the regex, so javascript: can't sneak in.
      const label = tok.slice(1, tok.indexOf("]"));
      const href = tok.slice(tok.indexOf("](") + 2, -1);
      out.push(
        <a key={key++} href={href} target="_blank" rel="noopener noreferrer" className="text-brass underline decoration-brass/40 hover:decoration-brass">
          {renderInline(label)}
        </a>,
      );
    }
    last = idx + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function splitRow(line: string): string[] {
  return line.replace(/^\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
}

const isTableDivider = (line: string) => /^\|?[\s:|-]+\|?$/.test(line) && line.includes("-");

export function Markdown({ text, className }: { text: string; className?: string }) {
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let key = 0;
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i++;
      continue;
    }

    // Headings
    const h = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (h) {
      const level = h[1].length;
      const cls = [
        "font-serif text-xl text-zinc-100 mt-6 mb-2 first:mt-0",
        "font-serif text-lg text-zinc-100 mt-6 mb-2 first:mt-0",
        "text-sm font-semibold tracking-wide text-zinc-200 mt-4 mb-1.5 first:mt-0",
        "text-sm font-medium text-zinc-300 mt-3 mb-1 first:mt-0",
      ][level - 1];
      const Tag = (["h2", "h3", "h4", "h5"] as const)[level - 1];
      blocks.push(<Tag key={key++} className={cls}>{renderInline(h[2])}</Tag>);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
      blocks.push(<hr key={key++} className="my-4 border-ink-800" />);
      i++;
      continue;
    }

    // Table: a pipe row followed by a divider row.
    if (trimmed.startsWith("|") && i + 1 < lines.length && isTableDivider(lines[i + 1].trim())) {
      const header = splitRow(trimmed);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(splitRow(lines[i].trim()));
        i++;
      }
      blocks.push(
        <div key={key++} className="my-3 overflow-x-auto rounded-lg border border-ink-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-ink-800 bg-ink-850/60 text-left text-xs uppercase tracking-wider text-zinc-500">
                {header.map((c, j) => (
                  <th key={j} className="px-3 py-2 font-medium">{renderInline(c)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((cells, r) => (
                <tr key={r} className="border-b border-ink-850 align-top last:border-0">
                  {cells.map((c, j) => (
                    <td key={j} className="px-3 py-2 leading-relaxed text-zinc-300">{renderInline(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Blockquote
    if (trimmed.startsWith(">")) {
      const quote: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quote.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote key={key++} className="my-3 border-l-2 border-brass/50 pl-3 text-sm leading-relaxed text-zinc-400">
          {renderInline(quote.join(" "))}
        </blockquote>,
      );
      continue;
    }

    // Lists (unordered - / * and ordered 1.)
    const isItem = (s: string) => /^([-*]|\d+\.)\s+/.test(s);
    if (isItem(trimmed)) {
      const ordered = /^\d+\./.test(trimmed);
      const items: string[] = [];
      while (i < lines.length && isItem(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^([-*]|\d+\.)\s+/, ""));
        i++;
      }
      const cls = "my-2 space-y-1.5 pl-5 text-sm leading-relaxed text-zinc-300";
      const children = items.map((it, j) => <li key={j}>{renderInline(it)}</li>);
      blocks.push(
        ordered ? (
          <ol key={key++} className={`${cls} list-decimal`}>{children}</ol>
        ) : (
          <ul key={key++} className={`${cls} list-disc`}>{children}</ul>
        ),
      );
      continue;
    }

    // Paragraph: merge consecutive plain lines.
    const para: string[] = [line];
    i++;
    while (i < lines.length) {
      const t = lines[i].trim();
      if (!t || /^(#{1,4})\s/.test(t) || t.startsWith("|") || t.startsWith(">") || isItem(t) || /^(-{3,}|\*{3,})$/.test(t)) break;
      para.push(lines[i]);
      i++;
    }
    blocks.push(
      <p key={key++} className="my-2 text-sm leading-relaxed text-zinc-300">
        {renderInline(para.join(" ").trim())}
      </p>,
    );
  }

  return <div className={className}>{blocks}</div>;
}
