# Production Deployment — M, behind the existing Cloudflare Tunnel

> **Status: NOT DEPLOYED.** This document describes the real target and the real procedure, but the
> stack has not been stood up in prod and no public hostname exists for it. Two decisions are open
> and belong to the owners (Jared + Joe) — see [Open decisions](#open-decisions) before doing
> anything in the Cloudflare section.

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
5. This repo at `/home/jared-williams/projects/3b. Robinhood Agentic`.

## Steps

### 1. Backend secrets

```bash
cd "/home/jared-williams/projects/3b. Robinhood Agentic"
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
cd "/home/jared-williams/projects/3b. Robinhood Agentic"
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

- Caddy basic-auth gates everything, plus security response headers and `Cache-Control: private,
  no-store` on `/api/*`. Basic auth alone is **not** considered sufficient for a public hostname —
  Cloudflare Access is mandatory before exposure (issue #17).
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
