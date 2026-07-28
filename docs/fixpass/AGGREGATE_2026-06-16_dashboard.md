# Fixpass Aggregate — Agentic Dashboard

Four independent reviewers, non-overlapping slices, shared written quality bar. Reviews:
`REVIEW_backend_core.md`, `REVIEW_debate_engine.md`, `REVIEW_frontend.md`, `REVIEW_infra_bridge.md`.

## Verdict table

| Reviewer scope | Verdict | BLOCKER | SHOULD-FIX |
|---|---|---|---|
| Backend core (config/routers/services) | REQUEST CHANGES | 2 | 2 |
| Debate engine | REQUEST CHANGES | 1 | 5 |
| Frontend | APPROVE WITH CHANGES | 1 | 3 |
| Infra + bridge security | APPROVE (should-fix) | 0 | 5 |
| **Total** | — | **4** | **15** |

## BLOCKERs (every one, explicit)

- **B1 [debate] Path traversal in `get_record`** — `records.py:94,102` ← `debate.py:63`. The
  `{record_id}` route param flows unvalidated into `debates_dir / f"{record_id}.json"`; a
  `%2F`-encoded `../` id reads any `*.json`/`*.md` in the container. **Highest severity.**
  Fix: regex-gate the id AND verify `target.resolve().is_relative_to(debates_dir.resolve())`.
- **B2 [backend] `/api/pipeline/run-stream` has no rate limit** — `pipeline.py:81-89`. Spends
  Anthropic tokens with no guard, while `debate.py` already gates with `debate_min_interval_seconds`.
  Fix: apply the same cooldown (ideally a shared limiter used by both).
- **B3 [backend] `/api/scan/run-stream` accepts unbounded user ticker lists** — `scan.py:23`. One
  request can fan out arbitrarily many blocking yfinance fetches (DoS). Fix: cap list length
  (e.g. ≤ 50) after validation.
- **B4 [frontend] pipeline error leaves the active node spinning forever** — `pipeline/page.tsx:59-61`.
  A `pipeline_error` shows a banner but never flips the running node to `error`; the stepper's
  error-icon branch is dead code. Fix: on `pipeline_error`, mark the in-flight node `error`.

## Top SHOULD-FIX (cross-cutting + highest impact)

- **SF-aggregate [debate] escalation over-fires** — `aggregate.py:36`. The even-split branch
  escalates for ANY two leading actions at N/2, so 5 BUY / 5 HOLD escalates like a BUY/SELL deadlock.
  Recommended: escalate only on a true **directional** deadlock (BUY & SELL each at N/2); a tie that
  includes HOLD resolves to HOLD (the conservative default for this live account). Add a test. (This
  refines 3a's "any 5-5 escalates" — document the divergence; fix-pass may reject with rationale.)
- **SF-refresh-atomic [backend]** — `refresh.py` trigger write is non-atomic + the cooldown is a
  lock-free TOCTOU read. Fix: write temp + `os.replace`; the cooldown race is low-risk on localhost
  but document or guard.
- **SF-cors [backend]** — wildcard CORS default. Default to localhost origins (any port) or document.
- **SF-scan-catch [frontend]** — `scan/page.tsx` has `finally` but no `catch` → silent failure on
  stream error. Add error surfacing like the debate page.
- **SF-timeout-leak [frontend]** — portfolio refresh 240s `setTimeout` never cleared (fires after
  unmount). Store in a ref; clear on unmount + on success.
- **SF-abort [frontend]** — no page passes the `AbortController` signal `streamSSE` already accepts;
  streams dangle on navigation / re-run. Wire an AbortController per streaming page.
- **SF-ports [infra]** — `pick_ports.sh` range `20000-59999` overlaps the Linux ephemeral floor and
  contradicts its own "biased below the floor" comment. Fix: `PORT_MAX=32767` (and soften the
  "guarantees freedom" wording given the residual TOCTOU).
- **SF-daemon-strict [infra]** — `refresh_daemon.sh` uses `set -u` only; add `pipefail` and document
  why `-e` is intentionally absent. Clean up the temp runner script after launch.
- **SF-up-health [infra]** — `up.sh` health loop prints the dashboard URL even if the backend never
  goes healthy. Detect the timeout and warn.

## PRAISE — do NOT undo in the fix-pass

- Read-only account architecture: no order-placement code path anywhere. API key never serialized to
  any response; snapshot pydantic-validated; marks soft-fail with NaN/non-positive rejection; all
  yfinance work off the event loop via `to_thread`.
- `streamSSE` is correctly buffered and partial-frame-safe; the TS type layer mirrors the backend
  Pydantic schemas with no contract drift; no XSS surface.
- Refresh bridge is least-privilege by construction: fixed prompt file (no external string in
  `claude` argv), `--allowedTools` limited to two read-only RH pulls + `Write` + `Bash(date*)`,
  `%q`-quoted tool array (injection-safe), wt.exe `;`/`,` gotcha mitigated via fixed-argv temp runner,
  clean secrets hygiene across `.gitignore`/`.dockerignore`/`.env.example`.
- Juror-failure containment (one bad juror → low-confidence HOLD, jury survives); forced-tool voting.

## Caveat
All four reviewers were sandbox-denied from running `ruff` / `pytest` / `npm run build` / `bash -n`,
so findings are static-analysis only. The fix-pass MUST run the suites after fixing.
