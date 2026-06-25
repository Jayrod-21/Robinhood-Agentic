# Independent Review — Debate Engine (3b Robinhood Agentic)

Reviewer: independent senior engineer (did not author this code). Read-only audit.
Date: 2026-06-16. Base: `/root/Jared/3b. Robinhood Agentic/`.

Scope: `backend/app/debate/{schemas,prompts,aggregate,anthropic_client,engine,records}.py`
plus `backend/tests/test_aggregate.py`, `backend/tests/test_records.py`. Context read:
`config.py`, `routers/debate.py`, `routers/pipeline.py`, `sse.py`, `src/data.py`.

---

## Summary verdict

**Changes requested — 1 BLOCKER.** The engine is well-structured, the asyncio orchestration
is correct, the juror-failure containment works, and the aggregation rules are almost entirely
right. The blocker is a **path-traversal in `get_record`**: the `{record_id}` URL parameter flows
unconstrained into a filesystem path, allowing read of arbitrary `*.json` / `*.md` outside the
debates directory via `%2F`-encoded `../` segments. There is also one **correctness gap** worth
fixing before approval (a BUY/HOLD or SELL/HOLD 5-5 split silently escalates as if it were a
BUY/SELL deadlock, which the tests do not exercise). Everything else is SHOULD-FIX or NIT.

I could not execute `pytest` (Bash run-command was denied in this session); the test analysis below
is by inspection. The four aggregate tests all assert correctly for the cases they cover, but they
leave the most error-prone branch (non-BUY/SELL ties, odd jury sizes) untested.

---

## Bar checklist

| Contract item | Status | Note |
|---|---|---|
| Majority threshold `jury_size//2+1` | PASS | `aggregate.py:20`, applied at `:25`. |
| Exact-even-split escalates | PARTIAL | Fires for any two leading actions at N/2, incl. BUY/HOLD — see SF-1. |
| Plurality → HOLD | PASS | `aggregate.py:48-54`. |
| Odd jury_size + tie-not-at-half | PASS | Escalation impossible for odd N (good); non-half ties → HOLD. |
| All-HOLD decisive | PASS | 10 HOLD ≥ majority → HOLD. Test covers it. |
| Juror retry → default HOLD | PASS | `engine.py:54-72`, 1 retry then HOLD, exception not re-raised. |
| One bad juror must not sink jury | PASS | Per-juror try/except, broad-but-justified `BLE001`. |
| Confidence clamp | PASS | `engine.py:61` `max(0, min(1, ...))`. |
| Vote enum coercion `.upper()` | PASS | `engine.py:60`. Invalid vote → ValueError → caught → retry/HOLD. |
| `as_completed` order re-sorted | PASS | `engine.py:149` `votes.sort(key=agent_id)` before aggregate. |
| Persistence off event loop | PASS | `engine.py:166` `asyncio.to_thread(persist_record)`. |
| Generator error events vs raised | PASS | Researcher failure yields `{"type":"error"}` then returns; no raise. |
| API key only via config, never logged | PASS | Read in `_client()` only; not in any log/response. |
| Forced tool_choice parse | PASS | `anthropic_client.py:59,62-65`; raises if no tool_use block. |
| Model ids valid | UNVERIFIED | `claude-haiku-4-5` / `claude-sonnet-4-6` — see SF-3; could not hit API. |
| Timeout / retry | PASS | `timeout=120.0, max_retries=2` at `anthropic_client.py:32`. |
| JSONL append concurrency | SHOULD-FIX | `records.py:39` plain append, no lock — see SF-2. |
| Path traversal on record_id | **FAIL** | **BLOCKER B-1.** |
| Prompt-injection surface (ticker) | NOTED | Acceptable; see N-3. |
| Type safety (pydantic v2) | PASS | Models clean; `dict` fields untyped but fine. |
| No dead code / debug prints | PASS | None found. |

---

## Findings

- **BLOCKER**
  - **B-1** Path traversal in `get_record` via `{record_id}` URL param. `records.py:94,102`.
- **SHOULD-FIX**
  - **SF-1** Escalation fires for non-BUY/SELL 5-5 ties (e.g. BUY/HOLD). `aggregate.py:36`.
  - **SF-2** JSONL event append is not concurrency-safe; interleaved writes can corrupt lines. `records.py:39-40`.
  - **SF-3** Model ids unverified and silently coupled to two config keys; one wrong alias → every call 404s with no fast feedback. `config.py:31,41`.
  - **SF-4** `list_records` / `get_record` re-read every JSON on each call with no cache and a broad except that hides schema drift. `records.py:52-57`.
  - **SF-5** Aggregate escalation keys on `len(votes)` not `jury_size`; correct only because the engine always returns exactly `jury_size` votes — a latent coupling. `aggregate.py:36`.
- **NIT**
  - **N-1** Two near-identical `TICKER_RE` + ticker-validation blocks across `debate.py` and `pipeline.py`. Duplicated logic.
  - **N-2** `_position_size_note` recomputes the BUY-confidence mean in a slightly awkward two-line form. `engine.py:80-83`.
  - **N-3** External ticker text is interpolated into prompts; low-risk but worth a one-line comment acknowledging the injection surface. `prompts.py:47-57`.
  - **N-4** `schemas.py` field ordering: `synth_model` declared after a validator method body in `config.py:41` (cosmetic, reads oddly).
- **PRAISE**
  - **P-1** Juror failure containment is exactly right: bounded retry, broad catch with a justifying comment, default low-confidence HOLD, jury survives. `engine.py:51-72`.
  - **P-2** `as_completed` for live streaming + a deterministic re-sort before aggregation — correct and non-obvious. `engine.py:144-149`.
  - **P-3** Forced `tool_choice` for a schema-validated vote instead of scraping free text. `anthropic_client.py:59`.
  - **P-4** Empty-string-API-key normalization so readiness checks and the debate gate agree. `config.py:33-40`.

---

## Detailed findings

### B-1 (BLOCKER) — Path traversal in `get_record`

`records.py:94` and `:102`:
```python
json_path = settings.debates_dir / f"{record_id}.json"
...
md_path = settings.debates_dir / f"{record_id}.md"
```
`record_id` arrives straight from the route `@router.get("/{record_id}")` (`debate.py:63-68`)
with **no charset/format validation** — contrast the ticker path, which is regex-gated.

The default Starlette `str` path converter does not match a raw `/`, but it **does** match a
percent-encoded slash. A request to
`GET /api/debate/..%2F..%2F..%2Fapp%2Fsome_secret` decodes after routing to
`record_id = "../../../app/some_secret"`, and `pathlib`'s `/` operator with `..` segments resolves
upward out of `debates_dir`. The `.json` / `.md` suffix constrains the extension but not the
directory, so this is an **arbitrary read of any `*.json` or `*.md` on the container filesystem**
(other records, mounted config, archived logs, anything an operator dropped with those extensions).
Read-only and extension-bounded, so not full LFI — but it is unauthenticated cross-directory read
on a paid trading dashboard, which fails the contract's explicit "likely BLOCKER if unguarded."

**Fix** (defense in depth — do both):
1. Validate `record_id` at the route or top of `get_record` against an allowlist, e.g.
   `re.fullmatch(r"[A-Za-z0-9._-]{1,80}", record_id)` and reject ids containing `..` or any path
   separator; 404 (not 400, to avoid an enumeration oracle) on failure.
2. After building the path, resolve and confirm containment:
   ```python
   base = settings.debates_dir.resolve()
   target = (base / f"{record_id}.json").resolve()
   if not target.is_relative_to(base):
       return None
   ```
   Apply the same to the `.md` branch. `is_relative_to` is 3.9+; this codebase is clearly ≥3.10.

The same unvalidated id reaches `list_records`'s archive path only via globbing (safe), so the fix
is localized to `get_record` + the route.

### SF-1 — Non-BUY/SELL ties escalate as if they were deadlocks

`aggregate.py:36`: `if top_n == second_n and top_n * 2 == len(votes):`

The docstring and the 3a rule frame escalation as the **5 BUY / 5 SELL** deadlock — two *opposed
actionable* verdicts with no majority. But this condition fires for **any** two leading actions at
N/2, including **5 BUY / 5 HOLD** or **5 SELL / 5 HOLD**. Because `sorted(reverse=True)` is stable
and `counts` is insertion-ordered `BUY, SELL, HOLD`, a 5-BUY/5-HOLD jury ranks BUY first, HOLD
second, hits the condition, and **escalates to a human** — when arguably the right outcome is HOLD
(half the jury already said HOLD; there is no actionable deadlock). This sends no-conviction
splits to a human queue and contradicts the "plurality short of conviction → HOLD" spirit.

This is a genuine semantic correctness gap, not just a preference, because it changes the decision
class (ESCALATED vs HOLD) on a live trading recommendation. It is **untested** — `test_five_five_tie`
only covers BUY/SELL.

**Fix:** restrict escalation to two *actionable, opposed* sides. Either require neither tied action
to be HOLD:
```python
if top_n == second_n and top_n * 2 == len(votes) and Decision.HOLD.value not in (top_action, second_action):
    ... escalate
```
or, if a BUY/HOLD deadlock genuinely should escalate, say so explicitly in the docstring and add a
test asserting it — right now the intent is ambiguous and the behavior is unverified.

### SF-2 — JSONL append not concurrency-safe

`records.py:39-40`:
```python
with settings.events_path.open("a") as fh:
    fh.write(json.dumps(event) + "\n")
```
`persist_record` runs under `asyncio.to_thread` (`engine.py:166`), so two debates finishing close
together execute this in **different threads** (and, under a multi-worker uvicorn, different
processes). A plain text-mode `write` of `data + "\n"` is not atomic; concurrent appends can
interleave and produce a corrupted JSONL line, which then trips the broad `except` in any reader.
The contract explicitly calls out "JSONL event append concurrency."

**Fix:** serialize the line first and write in one call under a lock, or rely on POSIX atomic
small-append semantics by opening in binary append and writing a single `bytes` payload `< PIPE_BUF`
(4096) in one `write`:
```python
line = (json.dumps(event) + "\n").encode()
with settings.events_path.open("ab") as fh:
    fh.write(line)
```
A single `write` of a sub-`PIPE_BUF` buffer to an `O_APPEND` fd is atomic on Linux, which is the
deployment target. For cross-process safety add an `fcntl.flock`. At minimum add a `threading.Lock`
to cover the in-process `to_thread` case, which is guaranteed today.

### SF-3 — Unverified model ids, no startup validation

`config.py:31` `jury_model="claude-haiku-4-5"`, `:41` `synth_model="claude-sonnet-4-6"`. I could not
hit the API to confirm these aliases resolve. If either alias is wrong, the failure mode is poor:
the researcher stage surfaces a generic `{"type":"error"}` (`engine.py:128`) and **every** juror
silently retries once then defaults to HOLD (`engine.py:64-72`) — i.e. a typo'd jury model yields a
unanimous 10-HOLD "decision" that looks legitimate rather than an obvious config error.

**Fix:** Verify both aliases against the current Anthropic model list before shipping. Consider a
lightweight startup or first-call validation, or at least promote a juror-stage all-failure into a
distinct error event so a bad model id is not laundered into a confident HOLD.

### SF-4 — `list_records` reload + broad except hides drift

`records.py:52-57`: every `/records` call re-reads and re-validates every `*.json` from disk, and a
`DebateRecord` schema change silently drops old records (logged at WARNING only). Fine at current
scale; flagging because (a) it is O(records) disk + parse per request with no cache and (b) the
broad `except Exception` can mask a real schema regression as "just a couple unreadable files."
Consider narrowing to `(ValidationError, OSError, json.JSONDecodeError)` so a programming error in
the model surfaces instead of being swallowed.

### SF-5 — Escalation keyed on `len(votes)`, not `jury_size`

`aggregate.py:36` uses `top_n * 2 == len(votes)`, while the majority on `:20` uses `jury_size`.
These agree only because the engine always appends exactly `jury_size` votes (failed jurors become
HOLD votes, still counted). If a future caller ever passes a short vote list, the majority and the
tie test would reference different denominators. Low risk today, but the two should use the same
basis — prefer `jury_size` (and assert `len(votes) == jury_size` at the top of `aggregate`).

---

## Coordination observations

- **Duplicated ticker validation (N-1):** `debate.py:22,31-35` and `pipeline.py:23,86-88` each
  define `TICKER_RE` and an upper/validate block. They have already drifted slightly (debate raises
  via a helper `_validate_ticker`; pipeline inlines it). Extract one shared `validate_ticker` so the
  paid path's input guard cannot diverge. This matters because `record_id` (B-1) shows what happens
  when one input on the same router is left unguarded.
- **Engine ↔ aggregate contract (SF-5):** the engine guarantees `len(votes) == jury_size`; aggregate
  half-relies on it. Make the invariant explicit (assert) so the two modules cannot silently
  disagree if the engine's juror handling changes.
- **Engine ↔ anthropic_client error taxonomy (SF-3):** `DebateUnavailable` is handled distinctly at
  `engine.py:123`, but a *wrong model id* is a generic exception that, for jurors, never even
  reaches the engine's error path — it is absorbed into default-HOLD. The two modules would benefit
  from a shared "config-level failure vs transient failure" distinction so misconfiguration cannot
  masquerade as a unanimous HOLD.
- **records ↔ router (B-1):** the persistence layer trusts its caller to hand it a safe id; the
  router does not validate it. Decide on one owner for id validation (recommend: validate at the
  router *and* contain in `records.py`, per B-1's two-part fix) rather than leaving the boundary
  ambiguous.

---

### Test-quality notes

- `test_aggregate.py` covers majority-BUY, 5-5 BUY/SELL escalation, plurality→HOLD, and unanimous
  HOLD — all assert correctly and none pass for the wrong reason. **Gaps:** no test for a non-BUY/SELL
  5-5 tie (would expose SF-1), no odd-`jury_size` test (e.g. 9), no SELL-majority test, and no
  non-half tie like 4/4/2 (which exercises the plurality→HOLD path *through* the escalation guard).
  Add these; SF-1 in particular is currently invisible to the suite.
- `test_records.py` covers `_date_from_stem`, `_first_heading`, and archive-markdown listing — solid.
  **Gaps:** no round-trip test of `persist_record` → `get_record` (the JSON path is entirely
  untested), and, critically, **no test asserting `get_record` rejects a traversal id** — add one
  alongside the B-1 fix (`get_record("../../etc/passwd")` and `get_record("..%2F..")` must return
  `None`).
