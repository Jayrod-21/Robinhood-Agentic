# Security Threat Model — Agentic Robinhood Dashboard

> **Who this is for.** Both owners (Jared, Joe) and anyone touching this code. As of August 2026
> this repo has two maintainers, so the security posture can no longer live in one person's head —
> this document is the shared, written version of it. It is built from the verified adversarial
> review in `docs/SECURITY_FINDINGS_2026-07-27.md` (finding numbers F1–F14 below refer to it) and
> from direct re-verification of the current tree on 2026-08-13. **Every "Defense" entry below was
> checked against the code before being written down. Where a defense does not exist yet, it says
> so, with the tracking issue number.** Closed by issue #8.

## 1. What we are actually protecting

The asset is **not** the ~$240 balance. Ranked:

1. **Order-placement authority against a live Robinhood account** (`••••4025`). The account
   balance is a parameter; the authority survives deposits. Today no order path exists in the app
   (§8) — but the host it runs on holds a Claude Code session whose robinhood-trading MCP *does*
   expose `place_equity_order`. Anything that can influence what that session runs is an attack on
   the brokerage account, not on a dashboard.
2. **The host-side Claude + MCP session.** The dashboard's refresh flow is an internet-facing
   button that ends with the host spawning a `claude` process holding an authenticated Robinhood
   OAuth token. That process's tool allow-list is a security control, not a convenience.
3. **`ANTHROPIC_API_KEY`** — loss means an attacker spends our money and impersonates the app.
4. **The holdings snapshot** (`data/account_snapshot.json`) — cash, buying power, every position,
   quantity, cost basis. Confidential, and also *trusted input*: debates and the twice-daily cycle
   reason over it, so tampering with it means poisoning decisions about real money.
5. **The market-data database** (`rh-db`) — evaluation integrity; a poisoned price history
   corrupts every backtest and paper-portfolio mark.

## 2. Trust boundaries and topology

**Dev stack (what runs today):** `docker compose up` — backend + frontend bound to
`127.0.0.1:<random port>` only (`docker-compose.yml:14,46`). **There is no authentication in the
dev stack**; the loopback bind *is* the access control, plus SSH to reach the box. That is a
deliberate, documented trade — do not weaken it by publishing a port.

**The bridge:** the container cannot reach the Robinhood MCP. `POST /api/refresh` only writes a
trigger file into the bind-mounted `data/`; the host-side daemon (`bin/refresh_daemon.sh`) acts on
it. The `data/` directory is therefore a trust boundary crossed in both directions.

**Prod target (documented, NOT deployed):** Caddy (basic auth + headers) on `127.0.0.1`, reached
only through the existing Cloudflare Tunnel; no hostname assigned yet. See `SERVER_DEPLOY.md`.

**Database:** separate compose project, no host port, egress-blocked internal network
(`docker-compose.db.yml`, ADR-001).

## 3. Attack vectors and defenses, per surface

Format per the standing rule: *what specific attack exists, and what stops it — or the honest
admission that nothing does yet.*

### 3.1 Network ingress and authentication

- **Vector:** anything on the LAN hits the unauthenticated dev API — reads full holdings, triggers
  host-side Claude spawns.
  **Defense (exists, verified):** both dev services bind `127.0.0.1` only
  (`docker-compose.yml:14,46`); fixed as F3. Remote use is via SSH tunnel.
- **Vector:** online password guessing against prod basic auth (no lockout, no rate limit,
  bcrypt-verified only), historically with the username pre-given as `admin`.
  **Defense (partial):** `deploy/.env.example` no longer ships a default username, and
  `SERVER_DEPLOY.md` makes Cloudflare Access mandatory before the hostname exists. **Open:** the
  lockout/rate-limit gap itself, and actually configuring Access — issue #17. Prod is not deployed,
  so today this surface does not exist.
- **Vector:** reaching the origin around Cloudflare.
  **Defense (by construction, for the documented target):** only Caddy binds a host port and only
  on loopback (`deploy/docker-compose.prod.yml`); no public DNS A record points at M.

### 3.2 Browser-borne attacks

- **Vector — CSRF, historically the highest open risk (F1, issue #11):** a cross-site
  auto-submitting form (a CORS *simple* request — no preflight, and CORS only blocks reading the
  response, not sending) queues a refresh that spawns the host-side Claude + MCP process. The same
  request shape becomes forced order placement if a Commit path ever ships.
  **Defense (implemented on this branch, 2026-08-13, verified):** one shared guard on every
  state-changing route — `enforce_same_origin` in `backend/app/main.py`, registered app-wide via
  `FastAPI(dependencies=...)` so a newly added router is covered without anyone remembering.
  Checks: JSON Content-Type required (kills the auto-submitting-form vector outright), then
  `Sec-Fetch-Site` (a forbidden header scripts cannot forge) must be same-origin/same-site/none,
  else `Origin` must fullmatch the CORS allow-list; violations 403 with a logged, named reason —
  never a silent block. Tests: `backend/tests/test_csrf_guard.py`. Issue #11 open pending review
  and closure; the structural fix (a `SameSite=Strict` session cookie, which basic auth cannot
  express) remains future work under #17's auth rework.
- **Vector — stored XSS via debate/model output rendered in the UI.**
  **Defense (exists, verified as a non-finding):** the frontend never uses
  `dangerouslySetInnerHTML`; React's default escaping applies.
- **Vector — clickjacking the refresh button on the public UI.**
  **Defense (prod path only, this change):** `Content-Security-Policy "frame-ancestors 'none'"` +
  `X-Frame-Options DENY` in `deploy/Caddyfile`. Untested against a live deployment (none exists);
  the dev stack has no header layer.
- **Vector — holdings cached by a browser or intermediary.**
  **Defense (prod path only, this change):** `Cache-Control: private, no-store` on all `/api/*`
  responses in `deploy/Caddyfile`, plus HSTS, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy` denies. **Deliberately not claimed:** a
  script-src CSP — Next.js hydration needs inline scripts, so an honest one requires nonce
  plumbing through the frontend; shipping `'unsafe-inline'` would be security theater. Follow-up
  under issue #16.
- **Vector — a malicious origin browsing the API cross-origin.**
  **Defense (exists, verified):** CORS never defaults to `*`
  (`backend/app/main.py:50-57`); localhost-regex only (Starlette `fullmatch`, so
  `localhost:1.evil.com` does not match), `allow_credentials=False`.

### 3.3 The refresh bridge and the host Claude + MCP session

- **Vector:** the refresh Claude session is tricked or misused into placing orders.
  **Defense (exists, verified):** the tool allow-list in `bin/refresh_once.sh:35-40` and
  `bin/refresh_daemon.sh` is read-only — `get_portfolio`, `get_equity_positions`, plus (tightened
  on this branch, issue #20 / F14 residual) `Write` scoped to exactly the snapshot file and `Bash`
  scoped to the exact `date -u` command instead of a loose prefix. No order tool is allowed, and
  `--dangerously-skip-permissions` is deliberately not used. Tests:
  `backend/tests/test_refresh_tool_scope.py`.
- **Vector:** any local account forges `data/refresh.request` — the daemon acts on the file's mere
  existence — bypassing the API cooldown; or reads/tampers the snapshot.
  **Defense (exists, verified):** F2 fixed — `data/` and `logs/` are `0700` (confirmed on disk),
  the backend container runs as the host uid so no world-writable chmod is needed
  (`docker-compose.yml:19`, `bin/up.sh:70`), and the snapshot itself is `chmod 600` after each
  refresh (`bin/refresh_once.sh:82`). This protects against *other unprivileged users*; anything
  running *as* the operator's own account is outside this model.
- **Vector:** refresh-button mashing spawns a stack of host terminal tabs/processes.
  **Defense (exists, verified):** cooldown + pending-trigger check under a lock (TOCTOU closed) in
  `backend/app/routers/refresh.py:86-119`; trigger written atomically so the daemon never parses a
  partial file.

### 3.4 The snapshot as untrusted input

- **Vector:** a tampered or malformed snapshot (negative quantities, absurd cash, wrong schema)
  flows into P&L math, debates, and — one day — a sell decision for a fabricated position.
  **Defense (exists on this branch, verified):** `backend/app/services/snapshot.py` now rejects
  non-positive quantities (`Field(gt=0)`), negative cash/buying-power (`NonNegativeFloat`), and
  any `schema_version` other than the one this reader supports (compared at load, refusal logged).
  Issue #19 (F13) is open pending owner verification/closure.

### 3.5 API key and spend abuse

- **Vector:** key leaks via repo, image, or API response.
  **Defense (exists, verified):** `backend/.env` is gitignored (`.gitignore:11`) and excluded from
  build context (`.dockerignore`); gitleaks runs in CI on every PR (`.github/workflows/gitleaks.yml`);
  the key is read only by the client constructor (`backend/app/debate/anthropic_client.py`) and
  never echoed. **Not claimed:** there is no repo-local `.gitleaks.toml`; scanning runs with the
  action's default ruleset. If one is ever added it MUST contain `[extend] useDefault = true` —
  without that line a custom config silently disables all default rules.
- **Vector:** a held button or client loop fans out unbounded paid debates.
  **Defense (exists, verified):** one shared cooldown limiter for *both* token-spending routes
  (debate + pipeline share a single budget, `backend/app/ratelimit.py`), lock-guarded against
  concurrent TOCTOU.

### 3.6 Input validation

- **Vector:** hostile ticker strings reach yfinance's URL interpolation.
  **Defense (exists on this branch, verified):** single shared validator, strict grammar
  `^[A-Z]{1,5}(\.[A-Z])?$` (`backend/app/validation.py:19`) — F12's consecutive/trailing-dot
  acceptance is fixed; issue #18 open pending closure.
- **Vector:** path traversal via debate record ids (`GET /api/debate/{record_id}`).
  **Defense (exists, verified):** allow-list regex rejecting `..`, `/`, `\` *plus* a
  resolved-path containment check — two independent layers.

### 3.7 Rate limiting and resource abuse

- **Vector:** concurrent scans fan out universe-sized yfinance fetches → host IP banned by Yahoo.
  **Defense (exists on this branch, verified):** scan cooldown limiter (separate from the paid
  budget, `backend/app/routers/scan.py:116`), pydantic hard cap of 500 tickers per request plus
  the tunable `scan_max_tickers` gate. F4/issue #12 fixed in-tree, pending closure. Per the
  guardrail rule, the limit is observable: the 429 says why and how long to wait.
- **Vector:** a runaway container starves the host (which also runs 9b and the GPU workloads).
  **Defense (this change, issue #16):** memory/cpu/pids caps + `memswap == mem` + log-size caps on
  every service in both composes; the db compose already had them.

### 3.8 Supply chain

- **Vector:** a floating `>=` dependency resolves to a compromised or vulnerable release on the
  next `--build`, inside a container holding the API key and snapshot.
  **Defense (exists on this branch, verified):** `backend/requirements.txt` is pinned to exact
  versions; CI runs `pip-audit --strict` over backend, screen, and db requirements, an `npm audit`
  gate over the frontend lockfile, and a Trivy image scan with pinned actions
  (`.github/workflows/ci.yml`, `image-scan.yml`); the Postgres image is digest-pinned
  (`docker-compose.db.yml:40`). Issue #15 (F8) fixed in-tree, pending closure. **Residual, honest:**
  pins are exact-version, not hash-pinned, for the backend (db/requirements.txt is hash-pinned);
  `yfinance` remains an unofficial scraper with direct egress and deserves continued scrutiny.

### 3.9 Logging and information disclosure

- **Vector:** upstream exception text (Anthropic SDK errors carry status + response body; our own
  errors have leaked config paths) streamed to clients.
  **Defense (exists on this branch, verified):** F5 fixed — the debate engine streams fixed
  generic messages and logs full detail server-side only (`backend/app/debate/engine.py:123-137`);
  `DebateUnavailable`'s message is kept client-safe by contract. Issue #13 pending closure.
- **Vector:** secrets or holdings land in logs that later get shared or exfiltrated.
  **Defense (implemented on this branch, 2026-08-13, verified):** `SecretRedactionFilter` in
  `backend/app/main.py` — attached to every root handler by the shared `configure_logging()`
  bootstrap, which the cron cycle imports too, so the API process and the twice-daily job run one
  implementation. It redacts `sk-ant-` keys, bearer tokens, and authorization/api-key header
  values, including in exception text (the F7 structural gap: an SDK regression attaching auth
  headers to an exception would otherwise land in `logger.exception`). The cycle also no longer
  logs held tickers — count only; per-ticker detail lives in the report files, which sit in the
  `0700` `logs/` tree. Tests: `backend/tests/test_log_redaction.py`. Issue #14 open pending
  closure. Additional mitigation: container log volume is rotation-capped.

### 3.10 Containers and host

- **Vector:** container escape / privilege escalation from a compromised dependency.
  **Defense (this change + prior, issue #16):** all images run non-root (backend uid 1001,
  frontend `nodejs`, verified in Dockerfiles); both composes now set `no-new-privileges:true` and
  `cap_drop: ALL` on every service (Caddy adds back only `NET_BIND_SERVICE`); backend and Caddy
  run read-only root filesystems with tmpfs `/tmp`. **Deliberately not claimed:** `read_only` on
  the frontends — `next dev` writes `/app/.next` continuously and `next start` writes
  `.next/cache`; a read-only rootfs there would break the app, so the composes say so in comments
  instead of pretending.
- **Vector:** secrets baked into image layers.
  **Defense (exists, verified):** `.dockerignore` excludes `.env`, `backend/.env`, `data/`,
  `logs/`; no `ENV`/`ARG` secret in any Dockerfile.

### 3.11 Database

- **Vector:** SQL payload exfiltrates via `COPY ... FROM PROGRAM 'curl ...'`.
  **Defense (exists, verified):** `rh-internal` is `internal: true` — no egress, DNS included
  (verified in ADR-001); no host port is published; on-box access faces scram-sha-256 with the
  credential in `db/.env` (0600).
- **Vector:** destructive or tampered migrations.
  **Defense (exists):** runner-owned transactions, checksums, filename-declared destructive gate
  (see `db/` and ADR-002). **Open, security-adjacent:** #34 (`ALTER DEFAULT PRIVILEGES` re-opens
  append-only for future tables), #31 (untrue comment about `rh_app` authentication), #30
  (migration lock blocks silently).

## 4. The order path that does not exist yet

Verified 2026-08-13: `place_equity_order` / `place_option_order` appear nowhere in `backend/`,
`src/`, or `bin/` except comments stating they are excluded. The app is read-only against the
account. **The following are hard gates before any Commit/order feature ships** (from the findings
doc's P2 list — none are implemented, because the feature they gate is not implemented):

1. Step-up re-auth **per order** + idempotency key as the fills-log primary key — a valid session
   must never by itself be sufficient to spend money.
2. Charter guardrails (≤25%/name, 10–20% cash floor, exit-before-entry, no averaging down)
   evaluated **in Python against the snapshot, server-side**, before any trigger is written. A
   prompt is not a control. Per the standing guardrail rule: tunable, observable, overridable —
   never a silent block.
3. HMAC-signed trade-trigger payload; a **separate** trade daemon with its own allow-list;
   `place_equity_order` never added to the refresh daemon's tools.
4. Close the prompt-injection surface on yfinance-sourced text before it can influence a tool that
   spends money (today the blast radius is bounded: the only model-callable tool is a JSON-emitting
   `cast_vote`).
5. Append-only fills log outside the bind-mount tree, `0600`; the structural CSRF fix (a
   `SameSite=Strict` session, #17) landed first — the header-based guard alone must not gate money.

## 5. Open gaps, honestly

| # | Gap | Severity |
|---|-----|----------|
| #17 | Basic auth: no lockout/rate limit; Cloudflare Access not yet configured (prod not deployed); the structural session-cookie auth rework | High (blocking exposure) |
| #16 | Remaining: full script-src CSP; frontend `read_only`; headers exist only on the (undeployed) Caddy path — the dev stack serves none | Medium |
| #22 | Live slate drifted from documentation — a risk-control gap, not code | Blocking (risk) |
| #34/#31/#30 | Database privilege/lock issues above | Medium |
| — | Backend pins are version-pinned, not hash-pinned | Low |

Fixed in this tree (branch `account-config-and-security-hardening`) but with issues still open
pending owner verification: #11 (CSRF guard), #12 (scan rate limit), #13 (error disclosure),
#14 (log redaction), #15 (dependency pinning + audits), #18 (ticker regex), #19 (snapshot schema),
#20 (daemon tool scope). Closing them is the owners' call after review.

## 6. Invariants for contributors

1. **Never publish a dev port beyond `127.0.0.1`**, and never re-introduce a blanket `chmod` on
   `data/` or `logs/`.
2. **Never add an order-placement tool to any refresh allow-list**, and never add
   `--dangerously-skip-permissions` to any script that runs `claude`.
3. **Every new state-changing route** is covered by the app-wide CSRF dependency automatically
   (`FastAPI(dependencies=...)` in `backend/app/main.py`) — never register a router on a separate
   app instance that bypasses it.
4. **Never stream `str(exc)` to a client.** Generic message out, full detail to the server log.
5. **New dependencies get exact pins** and must pass the CI audit gates in the same commit.
6. **A claimed defense needs a verification** — a test, or at minimum a file:line this document can
   cite. Do not write "defended" for behavior you have not read.
