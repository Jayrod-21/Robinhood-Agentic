// Types + a dev fixture for the Learning surface (issue #145): see what the system got right, what
// broke, and what silently didn't happen. The through-line the issue names is the defect this project
// keeps rediscovering, an ABSENT measurement impersonating a taken one, so every shape here keeps a
// gap distinct from a zero and a rate attached to its denominator.
//
// Mirrors the PROPOSED `GET /api/learning` contract in docs/contracts/learning-endpoint.md, which the
// backend computes from the scored judgments (accuracy by decision / lens / model family), the job
// health records, the abstention + reconciliation counts, and the engine-version marker. Nothing in
// this file exists on the backend yet; it is built mock-first behind NEXT_PUBLIC_LEARNING_MOCK so the
// page and the contract can land before the endpoint does (same pattern as perf/calibration/market).
//
// Not in lib/types.ts on purpose (same reason as perf/calibration/data-trust): that file mirrors
// shapes a real backend returns TODAY, and this endpoint does not exist yet. Move these over once it
// ships.

export type Decision = "buy" | "sell" | "hold";

/** A rate with its denominator ALWAYS attached. `accuracy` is null below `min_n` (issue §"Sample size
 *  beside every rate": 22 judgments said 4.5% correct and the number was meaningless). Null is ABSENT,
 *  the page renders it as such, never as 0%. */
export interface AccuracyRow {
  /** Decision ("buy"/"sell"/"hold"), lens name (focus_area), or provider ("anthropic"/"google"). */
  key: string;
  n: number;
  correct: number;
  accuracy: number | null;
}

export interface ScoringSection {
  /** Below this a bucket's accuracy is null: too few scored decisions to mean anything. */
  min_n: number;
  /** All judgments on record, scored or not. */
  total_judgments: number;
  /** Scored so far (a real, closed outcome). */
  scored: number;
  /** Scoring window elapsed but never scored. ABSENT, never counted wrong. This is exactly the
   *  "dead cron" failure mode #145 exists to surface. */
  unscored_absent: number;
  /** Window not yet closed, so legitimately unscored (not a gap, just not due). */
  pending_window: number;
  by_decision: AccuracyRow[];
  by_lens: AccuracyRow[];
  /** By model family (#142 made this measurable by recording `provider` per vote). */
  by_family: AccuracyRow[];
  /** Overall accuracy over time; each point carries its own n so a spike on three samples reads as
   *  noise, not signal. */
  trend: { period: string; n: number; accuracy: number | null }[];
}

/** Compact calibration read; the full reliability diagram stays on /calibration. */
export interface CalibrationDigest {
  is_calibratable: boolean;
  n: number;
  /** Realized fraction correct (the base rate). */
  base_rate: number | null;
  /** Mean stated confidence. base_rate < mean_confidence ⇒ systematically overconfident. */
  mean_confidence: number | null;
  /** Expected calibration error; null below the gate. */
  ece: number | null;
}

/** ok = ran on schedule; stale = overdue, last run too long ago; never = no record it EVER ran;
 *  failed = ran and errored. "never" and "stale" are the ones a log file's mere existence hides. */
export type JobStatus = "ok" | "stale" | "never" | "failed";

export interface JobHealthRow {
  name: string;
  status: JobStatus;
  last_run: string | null;
  /** Human cadence, e.g. "nightly 02:00 MT". */
  schedule: string | null;
  detail: string | null;
}

export interface GapsSection {
  jobs: JobHealthRow[];
  /** Debates where one or more jurors abstained. Before #141 an outage and a deliberation were
   *  indistinguishable in the record; this is the count that separates them. */
  debates_with_abstentions: number;
  total_abstentions: number;
  /** Cycles that never reconciled. Migration 024 keeps NULL distinct from false for exactly this, so
   *  "never ran" is not read as "ran and found no drift". */
  unreconciled_cycles: number;
  /** Judgments whose scoring window elapsed but were never scored (same figure as
   *  scoring.unscored_absent, surfaced here as a gap too). */
  unscored_judgments: number;
}

/** Accuracy tied to the engine version that produced the verdicts, so "did the change help" is
 *  answerable. The issue notes nothing currently ties a verdict to its engine version; this is the
 *  cheap schema marker it asks for, surfaced. */
export interface EngineVersionRow {
  version: string;
  /** First and last verdict produced on this version; `to` null means it is current. */
  from: string;
  to: string | null;
  n: number;
  accuracy: number | null;
  /** What materially changed in this version. */
  note: string | null;
}

export interface LearningMeta {
  generated_at: string;
  /** Days of scored history behind these numbers. One regime is not a track record. */
  window_days: number | null;
  /** Backend-owned caveat prose shown at the top, e.g. the "nine days of a mixed tape" warning. */
  regime_note: string | null;
  current_engine_version: string | null;
}

export interface LearningResponse {
  meta: LearningMeta;
  scoring: ScoringSection;
  calibration: CalibrationDigest;
  gaps: GapsSection;
  engine_versions: EngineVersionRow[];
}

// ── Dev fixture ────────────────────────────────────────────────────────────────────────────────
// Strict gate (NEXT_PUBLIC_LEARNING_MOCK=1); the default hits the real endpoint. The numbers are the
// real ones from issue #145 where it quotes them (the sell row is genuinely anti-predictive at 24.6%,
// the panel is overconfident, two crons died silently), so the page can be judged against a payload
// that looks like what it will actually show.
export const LEARNING_MOCK = process.env.NEXT_PUBLIC_LEARNING_MOCK === "1";

const GENERATED_AT = "2026-08-29T13:00:00Z";

function row(key: string, n: number, accuracy: number | null): AccuracyRow {
  return { key, n, correct: accuracy == null ? 0 : Math.round(n * accuracy), accuracy };
}

export const MOCK_LEARNING: LearningResponse = {
  meta: {
    generated_at: GENERATED_AT,
    window_days: 9,
    regime_note:
      "Every number here comes from nine days of one mixed-up tape. One regime is not a track record; read directions, not decimals.",
    current_engine_version: "2026.08.3",
  },
  scoring: {
    min_n: 30,
    total_judgments: 2354,
    scored: 814,
    unscored_absent: 340,
    pending_window: 1200,
    by_decision: [
      row("hold", 449, 0.751),
      row("sell", 321, 0.246), // anti-predictive; the most actionable number the project has produced
      row("buy", 44, 0.841),
    ],
    by_lens: [
      row("wasden_framework", 121, 0.62),
      row("valuation", 118, 0.585),
      row("sentiment", 116, 0.5), // votes as often as it is right
      row("momentum", 112, 0.464),
      row("macro", 24, null), // below min_n: shown as absent, not as a rate
    ],
    by_family: [
      row("anthropic", 410, 0.601),
      row("google", 404, 0.547),
    ],
    trend: [
      { period: "Aug 21-23", n: 96, accuracy: 0.53 },
      { period: "Aug 24-26", n: 402, accuracy: 0.561 },
      { period: "Aug 27-29", n: 316, accuracy: 0.598 },
    ],
  },
  calibration: {
    is_calibratable: true,
    n: 814,
    base_rate: 0.557,
    mean_confidence: 0.712, // > base rate ⇒ overconfident
    ece: 0.155,
  },
  gaps: {
    jobs: [
      { name: "score_judgments", status: "ok", last_run: "2026-08-29T09:05:00Z", schedule: "hourly", detail: null },
      { name: "stack_health", status: "ok", last_run: "2026-08-29T12:30:00Z", schedule: "every 30m", detail: null },
      { name: "nightly_marks.sh", status: "failed", last_run: "2026-08-27T02:00:00Z", schedule: "nightly 02:00 MT", detail: "failed at line 7 of the env file, before its own failure-reporting code" },
      { name: "alpaca_sync.sh", status: "failed", last_run: "2026-08-27T02:10:00Z", schedule: "nightly 02:10 MT", detail: "same env-sourcing failure; silent for two days" },
      { name: "reconcile_cycle", status: "stale", last_run: "2026-08-26T04:00:00Z", schedule: "daily 04:00 MT", detail: "last successful run 3 days ago" },
      { name: "weekly_digest", status: "never", last_run: null, schedule: "weekly Mon", detail: "no record it has ever run" },
    ],
    debates_with_abstentions: 3,
    total_abstentions: 14,
    unreconciled_cycles: 2,
    unscored_judgments: 340,
  },
  engine_versions: [
    { version: "2026.08.1", from: "2026-08-20", to: "2026-08-24", n: 421, accuracy: 0.519, note: "unanchored confidence (59% of votes were 0.72), single-family jury" },
    { version: "2026.08.2", from: "2026-08-24", to: "2026-08-27", n: 262, accuracy: 0.565, note: "anchored confidence + degeneracy detection (#139)" },
    { version: "2026.08.3", from: "2026-08-27", to: null, n: 131, accuracy: 0.603, note: "paired Claude/Gemini panel (#142), abstentions + quorum (#141)" },
  ],
};
