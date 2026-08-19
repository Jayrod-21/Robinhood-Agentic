// Types plus a dev-only fixture for the in-site Claude chat: a drawer where the operator asks Claude
// about debates/models/portfolio, and Claude can PROPOSE a tunable-weight change that the operator
// confirms. v1 scope (per the plan and the threat model): analyze + propose settings changes only,
// each human-confirmed. NO trades: the order path stays dark, and an agent that could both loosen a
// guardrail and act on it is out of scope until a dedicated threat-model addendum and the auth cutover.
//
// Frontend owns the drawer + the confirm UX; the backend (Jared) runs the server-side Anthropic
// tool-use loop and executes a confirmed proposal via the existing PUT /api/settings/{key} (already
// bounded, validated, and attributed to the operator). Contract: docs/contracts/chat-endpoint.md.

export type ChatRole = "user" | "assistant";

// pending  : proposed, awaiting the operator's confirm/dismiss
// applied  : confirmed; the backend wrote it via PUT /api/settings/{key}
// dismissed: the operator declined
export type ProposalStatus = "pending" | "applied" | "dismissed";

/** A single tunable-weight change Claude proposes. It is never applied without an explicit confirm,
 *  and it maps one-to-one to a key in the settings registry (settings_store.py), so the write is the
 *  same bounded/audited PUT the Parameters page uses. */
export interface SettingProposal {
  key: string;
  label: string;
  current: number;
  proposed: number;
  unit: string | null;
  /** Why Claude is proposing it, in one or two sentences. Shown on the confirm card. */
  rationale: string;
  status: ProposalStatus;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  /** Markdown; rendered through components/markdown.tsx (same renderer as debate output). */
  content: string;
  /** Present on an assistant message that proposes a settings change; drives the confirm card. */
  proposal?: SettingProposal | null;
}

// ── Dev fixture ──────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_CHAT_MOCK=1); the default hits POST /api/chat. The seeded thread shows the
// two things v1 does: an analysis answer, and a proposal with a live confirm card.
export const CHAT_MOCK = process.env.NEXT_PUBLIC_CHAT_MOCK === "1";

export const CHAT_MODEL_LABEL = "Sonnet";

export const MOCK_THREAD: ChatMessage[] = [
  {
    id: "m1",
    role: "user",
    content: "Why is cash so high right now, and is that a problem?",
  },
  {
    id: "m2",
    role: "assistant",
    content:
      "Cash is **38.6%** of the account against a target band of **10 to 20%** (`docs/SLATE.md`), so the book is under-deployed. Two things are driving it:\n\n" +
      "- The reconciliation view shows **5 names drifted well under target** (TSM, VST, V, CVX, NVDA), so weight that should be invested is sitting in cash.\n" +
      "- **GEV and PLTR are documented in the slate but not held**, so their ~11% of intended weight is uninvested too.\n\n" +
      "It is not dangerous, but it is a drag: the slate expects that cash working. The cleanest fix is recording the missing entries and re-basing the slate, which is a decision for you, not a weight I can change.",
    proposal: null,
  },
  {
    id: "m3",
    role: "user",
    content: "The drift tolerance feels loose. Tighten it a little.",
  },
  {
    id: "m4",
    role: "assistant",
    content:
      "Reasonable. At **1.5 points** a 3% target can drift 50% before it flags, which is generous. I'd propose **1.0 point**, which still tolerates normal price movement but catches a leg that has quietly halved. You confirm before anything changes.",
    proposal: {
      key: "drift_tolerance_pct",
      label: "Drift tolerance",
      current: 1.5,
      proposed: 1.0,
      unit: "pts",
      rationale: "1.5 pts is 50% of a 3% target; 1.0 pt still absorbs normal moves but flags a halved leg sooner.",
      status: "pending",
    },
  },
];
