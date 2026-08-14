# Production Deployment — M, behind the existing Cloudflare Tunnel

> **Status: DEPLOYED 2026-08-13.** Live at **`ww.jaredstudio.com`** ("ww" for Wasden Watch), served
> through the existing named tunnel to Caddy on `127.0.0.1:1855`. The prod compose stack
> (`deploy-backend-1`, `deploy-frontend-1`, `deploy-caddy-1`) runs alongside the dev stack, which
> keeps its own loopback ports.
>
> **The gate is still a single shared basic-auth credential** — one username and password used by
> both owners. Cloudflare Access was specified as the compensating control and was deliberately
> NOT configured; issue #17 records that acceptance and the conditions that void it. The app is
> read-only with no order-placement path anywhere, so the realistic worst case from a guessed
> credential is disclosure of holdings, not trading. That calculation changes the day an order path
> ships, and `docs/AUTH_THREAT_MODEL.md` specifies the replacement.
>
> **2026-08-13 update: the replacement is now built** — per-operator Argon2id+TOTP auth, migrated
> into `rh-db`, 241 backend + 194 db tests. **It has not been cut over.** The running prod
> containers have not been rebuilt onto this code and no operator has been seeded. See
> [Operator authentication](#operator-authentication--onboarding-and-cutover-issue-17) below for the
> onboarding runbook and the cutover procedure — basic-auth stays in place until a real end-to-end
> login is proven against the deployed stack.

The production target is **this machine (M)** — the same box the dev stack, the database, and 9b
Korean Master already run on. There is no separate server. Public reachability comes from the
**named Cloudflare Tunnel that already runs on M** as a systemd service (config:
`/etc/cloudflared/config.yml`), which currently serves `korean.jaredstudio.com`,
`uvrl.jaredstudio.com`, and `uvrl-study.jaredstudio.com`. 3b becomes a fourth ingress rule on that
tunnel — it does **not** create a tunnel of its own.

The porting rationale (what we take from 9b's battle-tested posture and why) is in
`docs/PATTERNS_FROM_9B.md`. The threat model this topology exists to serve is in `SECURITY.md`.

## Topology

```
                Cloudflare edge (TLS terminates here)          M (this box)
  you ──https──▶ <hostname-TBD>.jaredstudio.com ──▶ cloudflared (existing systemd service)
                                                        │
                                                        ▼
                                            Caddy  127.0.0.1:${DASH_PORT}
                                            (basic auth + security headers)
                                              ├── /api/*  ──▶ backend  (FastAPI, no host port)
                                              └── /*      ──▶ frontend (Next.js prod build, no host port)
                                                            data/ + logs/ (bind-mounted, 0700)
  cron (open/close) ──▶ bin/scheduled_cycle.sh
        ├─ host: bin/refresh_once.sh ──▶ claude + robinhood MCP ──▶ data/account_snapshot.json
        └─ container: python -m app.jobs.cycle ──▶ scan + debates + logs/reports/
```

Posture, ported from 9b (see `docs/PATTERNS_FROM_9B.md` §5):

- **Only Caddy binds a host port, and only on 127.0.0.1.** Backend and frontend have no host ports
  at all; the database publishes nothing (`docker-compose.db.yml`).
- **TLS terminates at the Cloudflare edge.** The origin speaks HTTP on loopback; nothing on the
  public internet can reach it except through the tunnel.
- **No public DNS A record points at M's IP**, so the origin cannot be reached around Cloudflare.
- Holdings come from the snapshot, refreshed by the host-side `claude` + MCP (no brokerage
  credentials are ever stored). **No order-placement path exists anywhere in the app** — verified;
  see `SECURITY.md` § "The order path that does not exist yet".

## Open decisions

Do not proceed past step 4 until the owners have decided:

1. **The hostname.** Nothing is assigned. `deploy/cloudflared-config.example.yml` shows the ingress
   rule shape with `<HOSTNAME-TBD>`. Constraint: single-label subdomain only
   (`something.jaredstudio.com`) — the Cloudflare certificate's wildcard does not match two labels.
2. **Cloudflare Access on the hostname.** Caddy basic-auth has no lockout and no rate limit
   (issue #17); over a public hostname that is unlimited online password guessing against the sole
   gate in front of a live brokerage view. This document treats Access as **mandatory, not
   optional** — it is the fastest real second factor available. Which identities to allow (both
   owners' emails) is part of the decision.

## Prerequisites (already true on M — verify, don't install)

1. **Docker + Compose v2** — `docker --version`, `docker compose version`.
2. **Claude Code** — installed and logged in (`claude` on PATH).
3. **robinhood-trading MCP at USER scope, authenticated** — this is what the twice-daily refresh
   rides on (step 3 below proves it).
4. **cloudflared** — already running as a systemd service for korean/uvrl. `systemctl status
   cloudflared` should say `active`.
5. This repo checked out somewhere on the host.

> **On paths in this document.** `/srv/agentic/robinhood-agentic` throughout is a **placeholder for
> wherever you cloned the repo** — substitute your own checkout path, including in
> `deploy/agentic-dashboard.service` and `deploy/crontab.example`, both of which need absolute
> paths because systemd and cron do not inherit a working directory.
>
> It is deliberately not a real path. An earlier version of these docs hard-coded one operator's
> home directory, which made every command fail for anyone else and is exactly what CI's
> repo-hygiene check now blocks.

## Steps

### 1. Backend secrets

```bash
cd "/srv/agentic/robinhood-agentic"
cp backend/.env.example backend/.env
# edit backend/.env → set ANTHROPIC_API_KEY=sk-ant-...   (jurors=haiku, synth=sonnet by default)
chmod 600 backend/.env
```

### 2. Dashboard auth (Caddy basic-auth)

```bash
cp deploy/.env.example deploy/.env
# generate a bcrypt hash for your password:
docker run --rm caddy caddy hash-password --plaintext 'YOUR-STRONG-PASSWORD'
# edit deploy/.env → DASH_USER=<non-obvious username>, DASH_PASSWORD_HASH=<that hash>, DASH_PORT=<free port>
chmod 600 deploy/.env
```

Pick a non-default username (the old example shipped `admin` — that was half the credential for
free), and **verify the port is actually free before binding it** — M hosts several live stacks:

```bash
ss -ltn "sport = :8088"    # no output = free; otherwise pick another port
```

### 3. Prove the Robinhood MCP refresh works headlessly

The scheduled refresh runs `claude --print` with a read-only tool allow-list
(`bin/refresh_once.sh`). The MCP's OAuth must have been completed once interactively; then:

```bash
bash bin/refresh_once.sh   # should print "✓ snapshot refreshed" and update data/account_snapshot.json
```

If it says "snapshot NOT updated", open `claude` interactively, confirm the robinhood tools are
available (re-OAuth if prompted), and retry. If the MCP is project-scoped instead of user-scoped,
set `AGENTIC_MCP_CWD` to that project dir.

### 4. Bring up the stack (local only — nothing public yet)

```bash
cd "/srv/agentic/robinhood-agentic"
docker compose -f deploy/docker-compose.prod.yml up -d --build
# verify locally (the ONLY way in until the tunnel rule exists):
curl -u USER:PASSWORD "http://localhost:${DASH_PORT:-8088}/api/health"
```

Everything up to this point is reversible and invisible from the internet. Stop here until the
[open decisions](#open-decisions) are made.

### 5. Cloudflare Tunnel — add 3b to the EXISTING tunnel

**This edits live shared infrastructure.** The tunnel also carries korean.jaredstudio.com and both
uvrl hostnames; a mistake here takes them down too. Follow
`deploy/cloudflared-config.example.yml` exactly — in short:

```bash
# 1. DNS route for the chosen hostname (CNAME to the tunnel):
cloudflared tunnel route dns <TUNNEL-NAME> <HOSTNAME>.jaredstudio.com

# 2. Back up, edit, validate, restart:
sudo cp /etc/cloudflared/config.yml /etc/cloudflared/config.yml.bak.$(date +%Y%m%d-%H%M%S)
# add the 3b ingress rule BEFORE the final http_status:404 rule (see the example file)
cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
sudo systemctl restart cloudflared
```

**`restart`, never `kill -HUP`** — the unit has no ExecReload; SIGHUP makes cloudflared exit
cleanly and systemd leaves it down (this darkened korean.jaredstudio.com for four minutes on
2026-08-11). The restart blips every hostname on the tunnel for a second or two; that is the whole
blast radius, and it is shared.

Then put **Cloudflare Access** in front of the hostname (Zero Trust dashboard → Access →
Applications) restricted to the owners' identities, **before** sharing the URL anywhere.

### 6. Start on boot

```bash
sudo cp deploy/agentic-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentic-dashboard
```

(The unit already carries M's real user and path.)

### 7. Schedule the twice-daily cycle

```bash
crontab -e     # paste deploy/crontab.example (paths are already M's)
crontab -l     # confirm
```

Test a run immediately:

```bash
AGENTIC_COMPOSE_FILE="$PWD/deploy/docker-compose.prod.yml" bash bin/scheduled_cycle.sh open
cat logs/cron/$(date -u +%Y%m%d)-open.log
cat logs/reports/$(date -u +%Y-%m-%d)-open.md
```

## Operator authentication — onboarding and cutover (issue #17)

**Status, 2026-08-13: built and migrated, NOT cut over.** `docs/AUTH_THREAT_MODEL.md` has the full
spec and the reconciled test-plan status. What follows is the runbook to actually stand it up. Do
not skip the precondition on the cutover step — Caddy basic-auth stays in place until a real login
has been proven end to end.

### A. One-time onboarding

1. **Set both database roles' passwords**, if not already done (they ship passwordless from
   migrations 001/012 and cannot authenticate until this runs — see the block comment above
   `DATABASE_URL`/`AUTH_DATABASE_URL` in `backend/.env.example`):
   ```bash
   bin/db_psql.sh -c "ALTER ROLE rh_app  WITH PASSWORD '<openssl rand -hex 24>'"
   bin/db_psql.sh -c "ALTER ROLE rh_auth WITH PASSWORD '<openssl rand -hex 24>'"
   ```
   Then set `DATABASE_URL` and `AUTH_DATABASE_URL` in `backend/.env` to match (`rh-db:5432`, no host
   port — ADR-001).

2. **Generate the TOTP encryption key** and put it in `backend/.env`:
   ```bash
   openssl rand -base64 32
   # → TOTP_SECRET_ENC_KEY=<the output>, chmod 600 backend/.env
   ```
   Back this up in the password vault immediately. If it is lost, every enrolled TOTP secret
   becomes undecryptable and every operator must re-enroll (`docs/AUTH_THREAT_MODEL.md` §7);
   `bin/manage_operator.py` validates it decodes to exactly 32 bytes and fails loudly, before any
   write, if it does not.

3. **Confirm the mail transport**, since nothing enforces this automatically at startup —
   `backend/app/services/email.py::assert_production_transport` exists to refuse booting prod on
   the mock transport, but it is **not currently wired into the app's startup path** (`lifespan` in
   `backend/app/main.py` does not call it). Check by hand: `SMTP_HOST` must be set in `backend/.env`
   (this deployment points it at Proton Mail Bridge, `host.docker.internal`) — an unset `SMTP_HOST`
   silently selects `MockEmailTransport`, which logs the verification link instead of emailing it,
   and there is currently no automated guard that would catch that in prod.

4. **Seed the first operator** — the only account-lifecycle surface; there is no signup route:
   ```bash
   bin/db_manage_operator.sh seed --email you@example.com
   ```
   This prompts for a password (8+ chars, ≤256 bytes, rejected if on the common-password block
   list) and prints, **once, to the terminal only**:
   - An `otpauth://` **provisioning URI** — this is text, not a rendered QR image. Either paste it
     into a QR generator to scan with an authenticator app (`echo "$URI" | qrencode -t ansiutf8`
     works if `qrencode` is installed), or add the account to the authenticator manually using the
     URI's `secret=` parameter.
   - 10 single-use recovery codes (Crockford base32, no I/L/O/U).

   Store both in the password vault immediately — this output is not persisted anywhere and is not
   recoverable; losing it means running `reset-totp` on the host.

5. **Verify the email address.** Seeding does not verify email by itself. Complete the
   `/verify-email` flow the app sends (fragment-linked token, per §5.6) before relying on the
   account.

6. **Repeat 1–5 for the second operator**, if this deployment is meant to serve both. Each operator
   is a fully separate account (§3.2 — independent revocation and attribution, deliberately not a
   shared login).

`bin/db_manage_operator.sh disable / unlock / reset-password / reset-totp --email …` cover the rest
of the lifecycle. All five subcommands are exercised by `db/tests/test_manage_operator.py`.

### B. Cutover — removing Caddy basic-auth

**Precondition, not optional: a successful end-to-end login through the real app must happen
first.** Do not remove `basic_auth` from `deploy/Caddyfile` on the strength of the tests passing —
the tests run against testcontainers and the FastAPI test client, not against the deployed prod
stack.

1. Rebuild and restart the prod stack so it actually picks up this work — checked directly on
   2026-08-13, the running `deploy-backend-1` predates it and has neither `AUTH_DATABASE_URL` in
   its environment nor the `auth_enforced` field in `/api/health`:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml up -d --build
   curl -u USER:PASSWORD "http://localhost:${DASH_PORT:-8088}/api/health"
   # confirm the response now includes "auth_enforced": true
   ```
2. With an operator seeded and email-verified (section A), log in through the real
   `/login` page over the tunnel hostname, complete the TOTP step, and confirm `/api/me` reflects
   the session and a protected route (e.g. the portfolio page) loads.
3. Confirm logout and re-login both work, and that a wrong TOTP code is rejected without granting
   a session.
4. **Only then** remove the `basic_auth` block from `deploy/Caddyfile`, redeploy Caddy, and confirm
   the dashboard is unreachable without a valid session cookie (`curl` with no cookie should now get
   401 from the backend on any non-`/api/health`/`/api/auth/*` route, fronted by Caddy with no
   basic-auth prompt).
5. Update `SECURITY.md` §5's #17 row and this file's status banner in the same change — a cutover
   that happens without the docs being updated in lockstep is exactly the failure mode this repo
   keeps hitting.

Cloudflare Access (see [Open decisions](#open-decisions)) remains a separate, still-open decision —
cutting over to per-operator auth does not by itself close that half of issue #17.

## Operations

- **Logs:** `docker compose -f deploy/docker-compose.prod.yml logs -f backend`; cron output in
  `logs/cron/`; cycle reports in `logs/reports/`; debate records in `logs/debates/`; event store
  `logs/events.jsonl`. Container logs are rotation-capped (10m × 5) so they cannot fill the disk.
- **Update:** `git pull && docker compose -f deploy/docker-compose.prod.yml up -d --build`.
- **MCP re-auth (the one recurring chore):** Robinhood OAuth tokens expire. If a scheduled refresh
  logs "snapshot NOT updated", run `claude` once to re-authenticate the robinhood MCP. The
  dashboard keeps working on the last snapshot + live prices until then.
- **Change the schedule:** edit the crontab times (they're US/Eastern via `CRON_TZ`).
- **Coexistence:** M also runs 9b (blue/green + shared Postgres), the uvrl stacks, MLflow, and
  `rh-db`. The prod compose carries memory/cpu/pids caps so this stack cannot starve them. Never
  prune stopped containers on M.

## Security posture (summary — the full model is `SECURITY.md`)

- Caddy basic-auth gates everything **today**, plus security response headers and
  `Cache-Control: private, no-store` on `/api/*`. Basic auth alone is **not** considered sufficient
  for a public hostname — Cloudflare Access is mandatory before exposure (issue #17). A
  per-operator replacement (Argon2id password + TOTP, `docs/AUTH_THREAT_MODEL.md`) is built and
  migrated but not yet cut over — see [Operator authentication](#operator-authentication--onboarding-and-cutover-issue-17)
  above for the runbook and the precondition on removing basic-auth.
- The Anthropic key lives only in `backend/.env` (gitignored, `chmod 600`) — never in an image
  layer or any API response.
- No Robinhood credentials are stored; the MCP OAuth token lives in Claude Code's own config on the
  host, and the refresh scripts run `claude` with a read-only tool allow-list.
- The account is read-only today: scans/debates/reports only. The order path does not exist, and
  `SECURITY.md` lists the gates that must precede it (step-up auth per order, server-side charter
  guardrails, signed trade triggers).
- Only Caddy is published, to `127.0.0.1` — the backend, frontend, and database have no host ports.
- All containers run with `no-new-privileges`, `cap_drop: ALL`, resource caps, and log rotation;
  backend and Caddy have read-only root filesystems.
