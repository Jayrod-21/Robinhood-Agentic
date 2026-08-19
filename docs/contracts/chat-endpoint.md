# Contract: `POST /api/chat` (Ask Claude drawer)

Feeds the chat drawer (`frontend/src/components/chat-drawer.tsx`, reachable from every page). Built and
rendering today against a dev fixture (`NEXT_PUBLIC_CHAT_MOCK=1`, see `frontend/src/lib/chat.ts`).
**v1 scope: analyze + propose tunable-weight changes the operator confirms. NO trades.**

This is the one feature in the plan that creates real new risk, so the safety section is not optional.
The frontend is safe to ship ahead of the backend (without the mock flag the drawer shows an honest
"not connected yet" state and can do nothing). The backend must not ship until the requirements below
are met.

## Shape

`POST /api/chat` runs a **server-side Anthropic tool-use loop** (Sonnet) and streams the reply. Reuse
`backend/app/debate/anthropic_client.py`'s client/timeout/forced-tool pattern and
`backend/app/sse.py::sse_response`; the frontend consumes it with `lib/api.ts::streamSSE` (same as the
debate stream). Body: `{ messages: [{role, content}], ... }`. Stream text deltas plus a structured
`proposal` event when Claude proposes a settings change.

A **proposal** matches `SettingProposal` in `lib/chat.ts`:

```jsonc
{
  "key": "drift_tolerance_pct",   // a key in the settings registry (settings_store.py)
  "label": "Drift tolerance",
  "current": 1.5, "proposed": 1.0, "unit": "pts",
  "rationale": "one or two sentences",
  "status": "pending"
}
```

A proposal is **never applied by the model**. The operator clicks Confirm, and only then does a
second call execute the write via the existing `PUT /api/settings/{key}` (already bounded, validated,
and attributed to `request.state.operator`). Dismiss discards it. Keep the confirm as a separate
request from the chat turn that proposed it.

## Tools (v1)

- **Read** (no confirm): get portfolio (`/api/account`), recent debates, calibration, current settings.
  These let Claude answer analytically.
- **`propose_setting_change`** (the only write path, and it does not write): returns a `SettingProposal`
  for the UI to render as a confirm card. The actual write is the operator-confirmed `PUT /api/settings/{key}`.
- **No trade tool.** The order path stays dark (`docs/EXECUTION_DESIGN.md` scopes out autonomous
  placement); it is not exposed here.

## Safety (hard requirements, do not ship without these)

1. **Attribute every write to `request.state.operator`**, exactly as `routers/settings.py` does. A change
   Claude applies is the operator's change, never an ambient or model identity.
2. **Do not ship a write-capable chat before the auth cutover.** Today `AUTH_DATABASE_URL` may be unset
   and `enforce_authenticated` stands down entirely (`docs/AUTH_THREAT_MODEL.md`). A chat that can change
   settings must not run in that posture.
3. **Treat all tool-returned content as untrusted** (prompt injection): debate text, market/Market-Mover
   text, and journal entries are attacker-influencable. An agent that reads them and can change settings
   is exactly the risk the threat model names. Keep the system prompt firmly in control and never let
   tool output escalate the model's permissions.
4. **One confirmed action at a time.** Forbid loosening a guardrail and acting on it in the same turn; a
   proposal to change a setting and any dependent action must be separately confirmed. (Trades are out
   entirely in v1, which removes the worst version of this, but keep the rule for when they return.)
5. Rate-limit the endpoint (`backend/app/ratelimit.py`) like the other token-spending routes.
6. Add a **chat section to `docs/AUTH_THREAT_MODEL.md`** before this ships.

## Model + cost

Sonnet by Joe's API token (Haiku is acceptable for cheaper turns; Sonnet is the default for reliable
tool use). Surface token usage the way the debate engine's accounting ContextVar already does, so a
chat session's cost is visible.

## Frontend done / handoff

- [x] Drawer on every non-public page (toggle, markdown responses, streaming-ready), `NEXT_PUBLIC_CHAT_MOCK=1` (in `ANY_MOCK`)
- [x] Confirm-card UX for a `SettingProposal` (Confirm / Dismiss, applied/dismissed states); "cannot place trades" disclaimer in-drawer
- [x] Honest "backend not connected yet" empty state when the flag is unset
- [ ] **backend:** `POST /api/chat` tool-use loop + the read tools + `propose_setting_change`; wire Confirm to `PUT /api/settings/{key}`; the six safety requirements above; the threat-model addendum
