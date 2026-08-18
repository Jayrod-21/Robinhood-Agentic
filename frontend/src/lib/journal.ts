// Types for the journal — `GET /api/history/entries` and the two write routes the page uses.
//
// WHAT THE JOURNAL IS FOR
//   The charter's sell-discipline rule says a held position with no written reason is broken by
//   definition, whatever the P&L says. `knowledge_base_entries` is where that reason lives, and it
//   is what a future debate reads back as track record. Until now it had endpoints, tables and
//   tests but no page, so nothing could be written through the app at all.
//
// APPEND-ONLY, AND THE UI MUST NOT PRETEND OTHERWISE
//   Migrations 004/011 grant inserts, not updates: a correction SUPERSEDES its predecessor rather
//   than editing it, so the record of what was believed at the time survives. That is why there is
//   no edit affordance anywhere on this page — offering one would imply history can be rewritten.

export type EntryType = "thesis" | "outcome" | "lesson" | "postmortem" | "note";

export interface JournalEntry {
  id: number;
  entry_type: EntryType;
  title: string;
  body: string;
  /** The takeaway, when the entry carries one. Outcomes and lessons usually do; theses may not. */
  lesson: string | null;
  /** Null for entries not tied to one name (a market-wide lesson, say). */
  symbol: string | null;
  portfolio_id: number | null;
  debate_id: number | null;
  agent_id: number | null;
  /** The id this entry corrects. Non-null means the older entry is superseded, never deleted. */
  supersedes_id: number | null;
  as_of: string;
}

export interface JournalResponse {
  entries: JournalEntry[];
  count: number;
}

export interface ThesisRequest {
  symbol: string;
  title: string;
  thesis: string;
}

export interface LessonRequest {
  title: string;
  lesson: string;
  body?: string | null;
  symbol?: string | null;
}

/** Display metadata per type. `tone` maps to the Badge component's keys. */
export const ENTRY_META: Record<EntryType, { label: string; tone: "buy" | "sell" | "hold" | "escalated" | "neutral" }> = {
  thesis: { label: "Thesis", tone: "buy" },
  outcome: { label: "Outcome", tone: "hold" },
  lesson: { label: "Lesson", tone: "neutral" },
  postmortem: { label: "Post-mortem", tone: "escalated" },
  note: { label: "Note", tone: "neutral" },
};

export const ENTRY_TYPES: EntryType[] = ["thesis", "outcome", "lesson", "postmortem", "note"];

/** Field limits, mirrored from the request models in backend/app/routers/history.py.
 *  Kept here so the form can refuse locally instead of round-tripping to a 422 the user
 *  cannot act on — but the backend is still the authority, not this. */
export const LIMITS = {
  title: 300,
  thesis: 20_000,
  lesson: 20_000,
  body: 50_000,
} as const;
