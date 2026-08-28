# Claude platform: what fits this app, and what doesn't

> **Status:** assessment, 2026-08-28. Nothing here is implemented. Tracked as an issue; this
> document is the reasoning behind it, written down so the decision does not have to be rediscovered.
>
> **Joe** — the batching item touches the scheduled cycle only and deliberately leaves the
> interactive debate stream alone, so it should not collide with the debate page. The caching item is
> a trap worth reading before anyone "adds prompt caching" to this repo.

## Context

Jared asked whether we should build "an agent" for 3b, and whether anything on
platform.claude.com / the Console would help. This is an architecture question, not a feature
request — so the deliverable is a recommendation with the reasoning, plus one concrete change worth
making.

Answered against current Anthropic platform documentation rather than from recall: the platform
moved substantially in 2025–26 and a remembered answer would likely have been wrong. Two of the
findings below (the 4096-token cache floor on Haiku 4.5, and Batches pricing) are specifically
things I would have gotten wrong from memory.

---

## Headline: no, don't build a Managed Agent

The skill's own decision table puts this app in the **Workflow** tier, not the Agent tier:

| Use case | Tier |
|---|---|
| "Multi-step pipelines with **code-controlled logic**" | Workflow — *"You orchestrate the loop"* |
| "Server-managed **stateful agent with workspace**" | Managed Agents |

The debate engine is a workflow by construction. It fans out N jurors with fixed prompts and
aggregates votes with deterministic Python (`aggregate()` counts votes; quorum, family-split and
calibration are all code). There is no open-ended, model-driven exploration anywhere in it — the
control flow is fully specified in advance, which is precisely the line the skill draws:

> *"only reach for agents when the task genuinely requires open-ended, model-driven exploration."*

Its "Should I Build an Agent?" gate fails on the first criterion (**Complexity** — the task is fully
specifiable) and the last (**Cost of error** — the output is a trading decision).

Migrating the engine to Managed Agents would mean handing Anthropic the orchestration we
deliberately own, losing the deterministic aggregation, and gaining a per-session sandbox this app
has no use for. It would be a large migration for negative value.

**Scheduled deployments** (CMA's cron replacement) are similarly not worth it: our cron is free,
already written, and the jobs it runs are mostly *not* model calls (bar loading, marking, scoring).

---

## What DOES fit: Message Batches for the jury fan-out

This is the one change worth making, and it fits almost exactly.

**Why it fits:**
- **50% cost**, no strings.
- Batches are asynchronous — and the scheduled cycle runs at 09:32 with **nobody watching**. Latency
  is genuinely free here.
- The jury is the right shape: after the bull/bear exchange completes, all 20 juror calls are
  **independent of each other** and share one transcript. That is a batch, not a pipeline.

**The boundary that matters:** batches must apply to the **scheduled cycle only**. The interactive
debate page streams `turn` and `juror_complete` events live (#141) — that live view is the feature
Jared just asked for, and batching would destroy it. So:

- `bin/scheduled_cycle.sh` → batch the jury
- `POST /api/debate/run-stream` → unchanged, stays streaming

**Files:** `backend/app/debate/engine.py` (the `asyncio.as_completed` fan-out at the jury step),
`backend/app/debate/anthropic_client.py` (a batch submit/poll path beside `cast_vote_attributed`).
Reuse the existing `_record_usage` → `app.llm.ledger.record` attribution so batched calls still land
in the cost split; results arrive **in any order** and must be keyed by `custom_id`, never position.

---

## What looks like a fit and is NOT: prompt caching

Worth writing down so nobody "adds caching" and believes it worked.

All 20 jurors read the **same** bull/bear transcript, which reads like a textbook caching case.
Two things kill it:

1. **The prompt is ordered wrong.** `juror_user_prompt` puts `Your lens: {lens}` at line 2 — *before*
   the transcript. Caching is a prefix match, so every juror's prompt diverges ~30 characters in and
   nothing after it is shared. Fixable by reordering (ticker → price → fundamentals → transcript →
   *then* lens and instructions).

2. **The jury model can't cache a prefix this small.** Minimum cacheable prefix is model-dependent
   and **not monotonic**:

   | Model | Minimum |
   |---|---|
   | Opus 5 / Fable 5 | 512 tokens |
   | Sonnet 5 / Sonnet 4.6 | 1024 |
   | **Haiku 4.5 (our `JURY_MODEL`)** | **4096** |

   Our shared prefix is ~2,000 tokens (transcript averages 1,590 chars × 4 turns, plus grounding and
   fundamentals). **Below Haiku 4.5's 4,096 floor, caching silently does nothing** —
   `cache_creation_input_tokens: 0`, no error, no warning.

So: reordering the prompt is cheap and correct, but do it *for its own sake*, and don't claim a cost
win. Caching only becomes real if the jury moves to Sonnet 5 or Opus 5, or if the transcript grows
past ~4K tokens (more rebuttal rounds would do that).

---

## Also worth knowing

- **Admin API — usage & cost reporting** (beta, since 2026-08-26). We hand-built `llm_usage` with
  estimated dollars from a documented price list, and PR #138 explicitly warns *"reconcile against
  the real invoice before settling up."* This is that reconciliation. Note: usage/cost reports are
  **raw HTTP only**, not in the SDKs, and need an Admin key (`sk-ant-admin…`).
- **Token counting** (`messages.count_tokens`) — could price a debate *before* running it, which
  feeds the guardrail-style "this cycle will cost ~$X" the cost work has been circling.
- **Structured outputs** (`output_config.format`) — the modern path for the juror vote; we currently
  force `tool_choice`. Not broken, so not urgent. Note the Gemini side already uses a response
  schema, so this would make the two providers structurally symmetric.

---

## Recommendation

1. **Don't** build a Managed Agent. Wrong tier; the skill's own gate says so.
2. **Do** batch the jury in the scheduled cycle — 50% off the largest line item, zero UX cost.
3. **Reorder** the juror prompt for prefix stability, honestly labelled as *not* a caching win yet.
4. **Later:** Admin API cost reconciliation, once there's enough spend to be worth checking.

This is advisory. Nothing here blocks item C (the book and the trade button), which is still the
open thread.

## Verification

- Batches: run one scheduled cycle with `--batch`, confirm 20 votes return, `custom_id` keyed
  correctly, `llm_usage` rows written with the right owner, and the verdict identical to a
  synchronous run on the same transcript.
- Caching claim: if anyone adds `cache_control`, assert `usage.cache_read_input_tokens > 0` in a
  test — the skill's own guidance is that a zero there means a silent invalidator or a
  below-minimum prefix.
