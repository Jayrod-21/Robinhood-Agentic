# Frontend Review — Agentic Robinhood Dashboard

**Reviewer:** Independent senior frontend reviewer (did not author this code).
**Scope:** `frontend/src/lib/{api,types,format}.ts`, `components/{ui,shell}.tsx`, `app/{layout,page,scan/page,pipeline/page,debate/page}.tsx`, `globals.css`, `tailwind.config.ts`, `package.json`, `tsconfig.json`, `Dockerfile`, `next.config.js`. Backend contract verified against `backend/app/routers/*.py`, `app/debate/{engine,records,schemas}.py`, `app/sse.py`.
**Build:** `npm run build` could not be executed — the sandbox denied Bash for `cd`/`npm` invocations. Review is static only. (Recommend the maintainer run `cd frontend && npm run build` to confirm compilation; nothing read suggests a type error, but this was not machine-verified.)

---

## Summary verdict

**APPROVE WITH CHANGES.** This is genuinely strong, senior-level work: the SSE parser is correct (buffered, partial-frame-safe, streaming decoder), the contract matches the backend almost exactly, the type layer mirrors the Pydantic schemas faithfully, and there is no XSS surface (no `dangerouslySetInnerHTML`, all LLM/user text rendered as React text children). No data-corruption or security BLOCKER.

The one item I am calling a **BLOCKER** is a correctness defect that is broken-by-construction against the documented contract: the pipeline page never consumes the `pipeline_error` node failure into node state, so a backend `pipeline_error` mid-run leaves the active node spinning forever (the stepper's own `error` status is dead code). That is wrong-state-on-screen, not just a missing nicety. Everything else is SHOULD-FIX (missing error handling on the scan stream, an uncleared 240s timer that can fire post-unmount, and zero `AbortController`/stream cleanup on navigation) or NIT.

---

## Bar checklist

| Bar item | Status | Notes |
|---|---|---|
| Every fetch/stream handles failure | ❌ | `scan/page.tsx` has no `catch` — `streamSSE` throw is an unhandled rejection with no error UI. |
| Loading states | ✅ | Spinners on all four pages; skeleton text on portfolio table. |
| Empty states | ✅ | Scan, debate history, pipeline, survivors all have explicit empty copy. |
| No unhandled promise rejections | ❌ | `scan` `run()` and `page.tsx` `onRefresh` paths (see findings). |
| AbortController on dangling streams | ❌ | `streamSSE` accepts a `signal` but **no caller ever passes one**; no unmount/navigation cleanup anywhere. |
| streamSSE buffer/partial-frame/decoder | ✅ | Correct. `{stream:true}` decoder, `\n\n` framing, buffer carried across reads. (One edge nit below.) |
| page.tsx refresh effect / 240s timeout cleanup | ❌ | `setTimeout(…, 240_000)` is never stored/cleared → fires after unmount or after early success. |
| debate vote accumulation (dedupe/double events) | ⚠️ | No dedupe; relies on backend emitting each `agent_id` once. Safe given current engine, but fragile (see NIT). |
| pipeline node state machine | ❌ | `pipeline_error` never sets a node to `error`; `NodeIcon` error branch unreachable (BLOCKER). |
| No dangerouslySetInnerHTML / XSS | ✅ | None. LLM text is text children only. |
| LLM/user text rendered as text | ✅ | Confirmed across debate/pipeline/scan. |
| No SECRET leaked to client | ✅ | Only `NEXT_PUBLIC_API_URL` (expected). No keys in frontend. |
| TS strict | ✅ | `strict: true` in tsconfig. |
| `any` usage justified | ⚠️ | `onEvent: (event: any)` + `ev.type` switches are pragmatic but untyped; a discriminated union would catch contract drift (SHOULD-FIX-lite). |
| No unsafe casts hiding contract drift | ⚠️ | `ev.survivors`, `ev.result`, `ev.jury`, `ev.vote` flow in untyped; `value as number` in donut is benign. |
| Dockerfile non-root | ✅ | Creates `nodejs` user, `USER nodejs`. |
| Dockerfile npm install vs ci | ⚠️ | Uses `npm install` with a lockfile present → should be `npm ci` (reproducible). |
| Accessibility (inputs/labels/buttons) | ⚠️ | Ticker/scan inputs are `placeholder`-only, no `<label>`/`aria-label`. Buttons are real `<button>`s (good). |
| No console.log left | ✅ | None found. |

---

## Findings

### BLOCKER
1. **Pipeline `pipeline_error` never transitions the running node to `error`** — `app/pipeline/page.tsx:59-61`. The node stays "running" (spinner forever) while the error banner shows. The stepper's own `error` status/`NodeIcon` X is dead code.

### SHOULD-FIX
2. **`scan/page.tsx` has no error handling** — `app/scan/page.tsx:27-38`. `streamSSE` rejects on a non-OK/down backend; there is `try…finally` but no `catch` and no error state → unhandled rejection, user sees nothing.
3. **Uncleared 240s refresh timer** — `app/page.tsx:38`. `setTimeout(…240_000)` is never captured or cleared; fires after early success (harmless flip) and after unmount (state update on unmounted component / React warning). Leak.
4. **No `AbortController` / stream cleanup on unmount or navigation** — `lib/api.ts:34-38` exposes `signal` but no page passes one. Navigating away mid-debate/pipeline/scan leaves the `fetch`+reader running and `setState` calls firing into an unmounted tree.
5. **Dockerfile `npm install` should be `npm ci`** — `Dockerfile:11`. Lockfile is present (`package-lock.json`, 71 KB). `npm install` can drift the tree; `npm ci` is the reproducible, contract-correct choice.
6. **`onEvent: (event: any)` and `ev: any` switches are untyped** — `lib/api.ts:37`; every page's stream handler. A discriminated-union event type per stream would let the compiler catch contract drift (e.g. a renamed `bull_case`). Acceptable as `any` for now, but this is the highest-value type-safety upgrade.

### NIT
7. **Inputs lack labels** — `app/debate/page.tsx:78`, `app/scan/page.tsx:48`, `app/pipeline/page.tsx:79`. Add `aria-label`.
8. **`error.message ?? error` on a typed SWR error** — `app/page.tsx:71`. `String(error.message ?? error)` is defensive but `error` is `any` from SWR; fine, just noisy.
9. **Vote accumulation has no dedupe** — `app/debate/page.tsx:46`, `app/pipeline/page.tsx:50-54`. A re-emitted `juror_complete` for the same `agent_id` would double-count. Backend emits once today; a `Map` keyed by `agent_id` would harden it.
10. **`streamSSE` ignores a final unterminated frame** — `lib/api.ts:55-78`. If the stream ends without a trailing `\n\n`, the last buffered event is dropped. Backend's `sse()` always appends `\n\n`, so this never bites in practice — worth a comment.
11. **`getJSON`/`postJSON` don't distinguish network errors** — `lib/api.ts:6-26`. A connection refusal throws a raw `TypeError: fetch failed`; surfaced text is fine but unfriendly. Minor.

### PRAISE
- **`streamSSE` is correctly implemented** — `lib/api.ts:51-78`: streaming `TextDecoder`, blank-line framing, buffer carried across reads, multi-`data:`-line tolerant, malformed-frame-tolerant. This is the highest-risk code and it is right.
- **Type layer faithfully mirrors the backend** — `lib/types.ts` matches `schemas.py` and the router response models (incl. `DebateSummary` ↔ `list_records()`, `ScanResult` ↔ `_screen_one`). No drift found.
- **No XSS surface, no secret leakage** — clean.
- **Genuinely distinctive design system** — `tailwind.config.ts` warm "ink/brass" palette, tabular-nums, `decisionTone`/`plColor` semantics. Not templated defaults.

---

## Detailed findings

### BLOCKER

**1. `pipeline_error` leaves the active node spinning — `app/pipeline/page.tsx:59-61`**
```ts
case "pipeline_error":
  setErr(ev.message);
  break;
```
Backend (`backend/app/routers/pipeline.py:76-78`) emits `{"type":"pipeline_error","message":...}` and `return`s — the currently-running node (`screen`/`bull`/`bear`/`jury`/`decision`) never receives a `node_complete`, so it is stuck at `status:"running"` with an animated spinner, while the error card shows above. The component even defines a `"error"` `NodeStatus` and an X icon (`pipeline/page.tsx:11`, `:133`) that are **unreachable**. Misleading UI: the user sees a perpetual spinner on a node that has actually failed.
*Fix:* on `pipeline_error`, flip every non-`completed` node to `"error"`. E.g.
```ts
case "pipeline_error":
  setErr(ev.message);
  setNodes((n) =>
    Object.fromEntries(
      Object.entries(n).map(([k, v]) =>
        v.status === "completed" ? [k, v] : [k, { ...v, status: "error" as NodeStatus }],
      ),
    ),
  );
  break;
```
(Optionally only mark the single `running` node, but failing-everything-not-done is clearer.)

### SHOULD-FIX

**2. Scan stream has no `catch` — `app/scan/page.tsx:27-38`**
```ts
try {
  await streamSSE("/api/scan/run-stream", …, (ev) => { … });
} finally {
  setRunning(false);
}
```
If the backend is down or returns non-OK, `streamSSE` throws (`lib/api.ts:46-49`) → unhandled rejection, and the user gets no feedback (button just stops spinning). Every other streaming page sets an `err` state. Add an error state + `catch` mirroring debate/pipeline:
```ts
const [err, setErr] = useState<string | null>(null);
// …
} catch (e) {
  setErr(e instanceof Error ? e.message : "Scan failed");
} finally { setRunning(false); }
```
and render an error `Card` like the other pages.

**3. Uncleared 240s timer — `app/page.tsx:38`**
```ts
setTimeout(() => setRefreshing(false), 240_000);
```
Never captured, never cleared. Two problems: (a) if the snapshot timestamp advances early (the success path, effect at `:22-27`), this timer still fires 4 min later — harmless flip but sloppy; (b) if the user navigates away, it fires `setRefreshing(false)` on an unmounted component. Store it in a ref and clear it both in the success effect and an unmount cleanup:
```ts
const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
// in onRefresh:
timer.current = setTimeout(() => setRefreshing(false), 240_000);
// in the success effect when generated_at advances, and in a useEffect(()=>()=>clearTimeout(timer.current!),[]):
if (timer.current) clearTimeout(timer.current);
```

**4. No `AbortController` / stream cleanup — `lib/api.ts:34-38` + all stream callers**
`streamSSE(path, body, onEvent, signal?)` already plumbs `signal` into `fetch`, but **no caller supplies one**. Navigating away (or unmounting) during a debate/pipeline/scan leaves the `ReadableStream` reader looping and `onEvent` calling `setState` into an unmounted component — React warnings at best, wasted backend work (and, for debate, wasted Anthropic tokens) at worst. Add an `AbortController` per run, abort it in a `useEffect` cleanup:
```ts
const ctrl = useRef<AbortController | null>(null);
useEffect(() => () => ctrl.current?.abort(), []);
// in run(): ctrl.current?.abort(); ctrl.current = new AbortController();
// pass ctrl.current.signal as the 4th arg; ignore AbortError in catch.
```
Note `streamSSE` does not special-case `AbortError`; an aborted stream will throw `AbortError` into the caller's `catch` and (in debate/pipeline) set it as `err`. When you wire abort, filter it: `if ((e as Error).name === "AbortError") return;`.

**5. `npm install` → `npm ci` — `Dockerfile:11`**
`COPY package.json package-lock.json* ./` then `RUN npm install`. With a committed lockfile, `npm ci` is the correct, reproducible install (fails if lock and manifest disagree, never mutates the lock). Switch to `RUN npm ci`. (Running `next dev` in the container is intentional per the header comment for runtime `NEXT_PUBLIC_API_URL` injection — that tradeoff is reasonable here, and non-root + `EXPOSE 3000` are correct.)

**6. Untyped stream events — `lib/api.ts:37` + page handlers**
`onEvent: (event: any)` defeats the type system exactly where contract drift would hurt (e.g. backend renames `bull_case`). Define per-stream discriminated unions and type the callback, so the `switch` is exhaustively checked:
```ts
type DebateEvent =
  | { type: "bull_complete"; bull_case: string }
  | { type: "bear_complete"; bear_case: string }
  | { type: "juror_complete"; vote: JurorVote; completed: number; total: number }
  | { type: "aggregate"; jury: JuryResult }
  | { type: "decision"; final_decision: string; position_size_note: string; reason: string }
  | { type: "error"; message: string }
  | { type: "debate_start" | "context" | "debate_complete"; [k: string]: unknown };
streamSSE<E>(…, onEvent: (e: E) => void, …)
```
Acceptable to ship as-is, but this is the single highest-value hardening for a system whose front/back contract is hand-synced.

### NIT
- **7. Input labels** — `debate/page.tsx:78`, `scan/page.tsx:48`, `pipeline/page.tsx:79`: add `aria-label="Ticker"` / `aria-label="Tickers to scan"`.
- **8. `String(error.message ?? error)`** — `page.tsx:71`: fine; SWR error is `any`, so the guard is justified, just verbose.
- **9. Vote dedupe** — `debate/page.tsx:46`, `pipeline/page.tsx:50-54`: append-and-sort with no key dedupe. Backend emits each `agent_id` once via `asyncio.as_completed` (`engine.py:144-147`), so safe today; a `Map<agent_id, vote>` would make it idempotent against retries/double-delivery.
- **10. Trailing-buffer at done** — `lib/api.ts:55-78`: a final frame without `\n\n` is dropped. Backend always frames with `\n\n` (`app/sse.py:`sse()`), so non-issue; add a one-line comment noting the assumption.
- **11. Network-error wording** — `lib/api.ts:6-26`: raw `fetch failed` surfaces to the UI on connection refusal. Cosmetic.

---

## Coordination observations

- **Contract is hand-synced, not generated.** `types.ts` currently matches `schemas.py` and the routers exactly — including the in-line decision payload (`final_decision`/`position_size_note`/`reason`, `engine.py:157-162`) and the pipeline node `data` shapes (`pipeline.py`). Because the link is manual and `any`-typed at the stream boundary (finding 6), any future backend field rename will fail silently in the browser. A shared generated type or at least the discriminated unions above would make the two services fail loudly instead.
- **`decision.reason` is emitted but unused** on the debate page (`debate/page.tsx:51-53` keeps only `final_decision`/`position_size_note`); the page instead shows `jury.reason` (`:106`). Not a bug — just note the backend sends a field the UI ignores.
- **Refresh is a host-daemon bridge** (`refresh.py` header): the 240s client timeout (finding 3) is the UI's only fallback when the daemon never runs. After fixing the leak, consider polling `GET /api/refresh/status` (already typed as `RefreshStatus` in `types.ts` but **never called**) to drive the spinner off real `pending`/`cooldown_remaining_s` instead of a blind 4-minute timer. That endpoint and type exist and are currently dead on the frontend.
- **Build not verified.** Sandbox denied `npm run build`; please run it once locally to confirm the `strict` build is green before merge.
