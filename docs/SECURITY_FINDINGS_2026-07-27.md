# Security findings — 2026-07-27

An adversarial review of 3b's current code against 9b Korean Master's threat models (see
`PATTERNS_FROM_9B.md` §5). Every finding below was verified against the source before being written
down; file:line references are to the tree as of commit `22c79eb`.

**Why the bar is high here.** The asset is not the ~$200 balance. It is **order-placement authority
against a live Robinhood account**, plus a host-side Claude session already OAuth'd to that broker.
Balance is a parameter; authority is the asset, and it survives deposits. Per
`SENIOR_ENGINEER_BAR.md` §7.2, this is live-money trading code.

## Assets

1. **Order-placement authority** over the Agentic account `••••4025`, via the host bridge, once the
   Commit button (`BACKLOG.md` item 2) exists.
2. **`ANTHROPIC_API_KEY`** — loss means an attacker spends our money and impersonates the app.
3. **The holdings snapshot** (`data/account_snapshot.json`) — cash, buying power, every position,
   quantity, and cost basis.
4. **The host-side Claude + MCP session.** The dashboard is an internet-facing button that spawns a
   process holding an authenticated Robinhood OAuth token. That process's tool allow-list is a
   security control, not a convenience.

---

## Fixed in this pass

### F2 — `data/` and `logs/` were world-writable  🔴

`bin/up.sh:62` ran `chmod -R a+rwX` on both trees, leaving directories `0777` and
`logs/events.jsonl` `0666` — confirmed on disk. Three consequences: any local account could read the
full financial position; any local account could **forge `data/refresh.request`**, which the daemon
acts on based purely on the file existing, bypassing the API's cooldown entirely; and any local
account could tamper with the snapshot so the dashboard, the debates, and the twice-daily cycle all
reason over fabricated holdings.

The root cause was a uid mismatch — the image runs as uid 1001 (`backend/Dockerfile:22-23`), the host
user is 1000, and `a+rwX` was the shortcut that made the bind mounts writable.

**Fix:** the backend now runs as the host user via `user: "${HOST_UID}:${HOST_GID}"` in
`docker-compose.yml`, so `data/` and `logs/` stay `0700`. Verified after the change: the container
reports `uid=1000` and can still write the mount.

### F3 — The dev stack published an unauthenticated API on `0.0.0.0`  🔴

`docker-compose.yml` bound both services with a bare `"${PORT}:8000"`. The dev stack has no Caddy and
therefore no auth of any kind — `backend/app/main.py` mounts six routers with no auth dependency — so
anything on the LAN that found the port got `/api/account` (full holdings) and `POST /api/refresh`
(spawns the host Claude + MCP session).

**Fix:** both bindings are now `127.0.0.1:${PORT}:...`. Reach the dashboard from another machine over
an SSH tunnel.

### F14 — `refresh_daemon.sh` pointed at a path from the retired WSL box  🟠

`bin/refresh_daemon.sh:36` hard-coded `MCP_CWD="/root/Jared"`. On M that path is not readable by the
daemon's user, so `cd "${MCP_CWD}" || exit 1` failed and every headless refresh exited 1 — a failure
mode indistinguishable from "the MCP isn't authenticated."

**Fix:** defaults to the project root, overridable via `AGENTIC_MCP_CWD`, matching
`bin/refresh_once.sh`. The tool allow-list in that script is *good* and should be documented as an
existing defense: read-only Robinhood pulls only, no order tool, and it deliberately avoids the
permission-skipping flag. Two residual gaps remain (below).

---

## Open

### F1 — CSRF on `POST /api/refresh`, undefended  🔴 highest open risk

`backend/app/routers/refresh.py:79` — `def request_refresh() -> RefreshResponse:` takes no parameters
and no body model, so FastAPI never inspects `Content-Type`. A cross-site auto-submitting form sends
`application/x-www-form-urlencoded`, which is a CORS **simple** request — no preflight — and the
browser attaches the ambient HTTP Basic credential. CORS blocks *reading* the response, not *sending*
the request. There is no `Origin` or `Sec-Fetch-Site` check anywhere in the codebase.

Effect today: any page the operator visits while authenticated can queue a refresh, spawning a
host-side `claude` process holding the authenticated Robinhood MCP. **The same shape becomes forced
order placement the moment the Commit button ships.** The debate and pipeline POSTs are safe only
incidentally — their JSON body makes a form post fail with 422.

Interim fix: one FastAPI dependency on every state-changing route requiring both
`Content-Type: application/json` and a same-origin `Origin`/`Sec-Fetch-Site`, `403` otherwise —
enforced in one place so a new router cannot forget it. Structural fix: a `SameSite=Strict` session
cookie, which HTTP Basic cannot express.

### F4 — `/api/scan/run-stream` has no rate limit  🟠

`backend/app/routers/scan.py` has no limiter, no cooldown, no gate. With an empty body it falls
through to `flat_universe()` and fans out one blocking yfinance fetch per name onto the threadpool.
`scan_max_tickers` bounds a **single request's** list, not the request **rate**, so N concurrent
scans are N × universe fetches — a path to getting the host IP-banned by Yahoo. The adjacent paid
endpoints get this right: `debate.py:37` and `pipeline.py:89` share one limiter deliberately. Scan
was simply missed.

### F5 — Upstream exception text streamed verbatim  🟠

`backend/app/debate/engine.py:128` yields `f"Researcher stage failed: {exc}"`, where `exc` is whatever
the Anthropic SDK raised — `APIStatusError.__str__` carries the upstream status and response body.
Same class at `engine.py:124` and `anthropic_client.py:26-28`, where `DebateUnavailable` discloses
the on-disk config path to an HTTP client. Map to a generic `502`.

### F6 / F7 — Holdings logged; no redaction filter  🟠

`backend/app/jobs/cycle.py:129` logs held tickers at INFO, twice a day, into `logs/cron/`. And
`main.py:18` / `cycle.py:23` are bare `logging.basicConfig` calls with no `Filter` — no redaction of
`sk-ant-`, `authorization`, or `api_key`. No current path logs the key, so F7 is a *structural* gap
rather than an active leak: an SDK regression that attaches an auth header to an exception would land
in `logger.exception` and, via F5, on the wire.

### F8 — Dependencies fully unpinned, no vulnerability scanning  🟠

`backend/requirements.txt` is entirely `>=`. No lockfile, no hashes. CI pins `ruff==0.16.0` (good) but
adds no `pip-audit`, no `npm audit`, no Dependabot. Since deploys are `docker compose up -d --build`,
the deployed dependency set is whatever `>=` resolves to that day, on a box holding an API key and a
brokerage snapshot. `yfinance` warrants extra scrutiny — unofficial scraper, fast release cadence,
direct network egress.

### F9 / F10 — No container hardening, no security headers  🟠

Neither compose sets `no-new-privileges`, `cap_drop: [ALL]`, `read_only`, resource limits, or
log-driver caps. (The Dockerfile *does* run non-root — genuinely good.) Neither Caddy nor FastAPI
sets HSTS, `X-Content-Type-Options`, CSP, `Referrer-Policy`, or `frame-ancestors`, and `/api/account`
— the full holdings response — carries no `Cache-Control: private, no-store`.

### F11 — Basic auth with no lockout, default username  🟠

`deploy/Caddyfile` verifies bcrypt but counts nothing, and `deploy/.env.example` ships
`DASH_USER=admin`. Over a public hostname that is unlimited online guessing against the sole gate.
Cloudflare Access is described as "optional" in both `SERVER_DEPLOY.md` and the cloudflared example
config; for a brokerage front-end it should be mandatory, and it is the fastest real second factor
available.

### F12 — `TICKER_RE` accepts consecutive and trailing dots  🟡

`backend/app/validation.py:17` — `^[A-Z][A-Z.]{0,5}$` matches `A..`, `AA...`, `A.....`. That string is
interpolated into a Yahoo URL path by `yf.Ticker(symbol)`. Low real-world severity — the target is
Yahoo, not our host — but it is the one input that leaves our network, and the intended grammar
(`BRK.B`) is `^[A-Z]{1,5}(\.[A-Z])?$`. The docstring already describes the tighter grammar the regex
over-delivers on.

### F13 — Snapshot schema permits impossible values  🟠

`backend/app/services/snapshot.py` — `quantity`, `cash`, and `buying_power` are unconstrained floats,
so negative quantities validate; `schema_version: int = 1` is a **default that is never compared**.
The module's own docstring correctly says the file "crosses a trust boundary." Once Commit exists, a
tampered snapshot means a fabricated position to sell.

### F14 residual — daemon tool scope  🟡

`Write` is unscoped in the allow-list, so the refresh Claude can write any path the host user can;
and `Bash(date*)` is a loose prefix pattern.

---

## Confirmed non-findings

Checked and clean — recorded so a future reviewer doesn't re-raise them:

- **CORS regex anchoring is fine.** Starlette 1.3.1 uses `fullmatch`, so `http://localhost:1.evil.com`
  does not match the dev regex.
- **No stored XSS via debate markdown** — the frontend uses no `dangerouslySetInnerHTML` anywhere.
- **No prompt-injection surface via `DebateRequest.question`** — it reaches the record and the SSE
  event only, never a prompt.
- **Path traversal on `GET /api/debate/{record_id}` is properly closed** — allow-list plus a
  resolved-path containment check.
- **The refresh TOCTOU is genuinely closed** by `_request_lock` in `refresh.py`.
- **`.dockerignore` correctly excludes** `.env`, `backend/.env`, `logs/`, `data/`.

---

## Prioritized remediation

**P0 — before the Cloudflare Tunnel goes public**
1. F1 interim CSRF gate (one dependency, all state-changing routes).
2. F11 Cloudflare Access on the hostname.
3. F4 cooldown limiter on scan.

**P1 — the Phase 1 baseline**
4. Replace basic-auth-only with Argon2id + opaque `SameSite=Strict` session + mandatory TOTP +
   5-strike/15-minute lockout + recovery codes, single seeded account. Keep Caddy basic-auth as the
   outer gate. This closes F1 structurally.
5. F5 generic `502` mapping · F6/F7 redaction filter and stop logging symbols · F8 hash-pinned
   lockfile + `pip-audit`/`npm audit` in CI + Dependabot · F9 container hardening in both composes ·
   F10 security headers + `Cache-Control` on `/api/account` · F12 tighten the regex · F13 constrain
   the schema and check `schema_version` · F14 scope the daemon's tools.

**P2 — gates `BACKLOG.md` item 2. Do not ship Commit without these.**
6. Step-up TOTP re-auth **per order** plus an idempotency key as the fills-log primary key — a valid
   session must not by itself be sufficient to spend money.
7. Charter guardrails (≤25%/name, 10–20% cash floor, exit-before-entry, no averaging down) evaluated
   **in Python against the snapshot**, server-side, before the trigger is written. A prompt is not a
   control. Tunable, observable, overridable — never a silent block.
8. HMAC-signed trade-trigger payload; a **separate** trade daemon with its own allow-list;
   `place_equity_order` never added to the refresh daemon's tools.
9. Close the prompt-injection surface on yfinance-sourced fundamentals (wrap third-party text in
   explicit delimiters the system prompt declares to be data). Today the blast radius is bounded
   because the only tool is a JSON-emitting `cast_vote`; Commit removes that bound.
10. Append-only fills log outside the bind-mount tree, `0600`.
11. Write `deploy/SECURITY.md` + `backend/SECURITY.md` from this document, then run the order path
    through its own `/fixpass`.
