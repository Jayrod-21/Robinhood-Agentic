# Server Deployment — Ubuntu, 24/7, twice-daily cycle

Run the Agentic dashboard on an Ubuntu server: always-on, reachable via Cloudflare Tunnel behind
basic auth, with a scheduled cycle at market open and close that refreshes the account, scans the
universe, debates each position, and writes a report.

## Topology

```
                 Cloudflare              Ubuntu server (this box)
  you ──https──▶ Tunnel ──▶ cloudflared ──▶ Caddy (127.0.0.1:8088, basic auth)
                                              ├── /api/*  ──▶ backend  (FastAPI, debate engine)
                                              └── /*      ──▶ frontend (Next.js, prod build)
                                                            data/ + logs/ (bind-mounted)
  cron (open/close) ──▶ bin/scheduled_cycle.sh
        ├─ host: bin/refresh_once.sh ──▶ claude + robinhood MCP ──▶ data/account_snapshot.json
        └─ container: python -m app.jobs.cycle ──▶ scan + debates + logs/reports/
```

Live prices/P&L come from yfinance inside the container. Holdings come from the snapshot, refreshed
by the host-side `claude` + MCP (no stored brokerage credentials). The account is **read-only** — no
order-placement path exists anywhere in the app.

## Prerequisites (install on the server)

1. **Docker + Compose v2** — `docker --version`, `docker compose version`.
2. **Claude Code** — installed and logged in to your Anthropic account (`claude` on PATH).
3. **robinhood-trading MCP at USER scope, authenticated once** (the crux — see step 3 below).
4. **cloudflared** — for the public tunnel.
5. This repo cloned to, say, `/home/YOU/agentic/3b. Robinhood Agentic`.

## Steps

### 1. Backend secrets
```bash
cd "/home/YOU/agentic/3b. Robinhood Agentic"
cp backend/.env.example backend/.env
# edit backend/.env → set ANTHROPIC_API_KEY=sk-ant-...   (jurors=haiku, synth=sonnet by default)
chmod 600 backend/.env
```

### 2. Dashboard auth (Caddy basic-auth)
```bash
cp deploy/.env.example deploy/.env
# generate a bcrypt hash for your password:
docker run --rm caddy caddy hash-password --plaintext 'YOUR-STRONG-PASSWORD'
# edit deploy/.env → DASH_USER=you, DASH_PASSWORD_HASH=<that hash>, DASH_PORT=8088
chmod 600 deploy/.env
```

### 3. Authenticate the Robinhood MCP (once, interactively)
The scheduled refresh runs `claude --print` headlessly, but the MCP's OAuth must be set up once with a
browser. Add it at **user scope** so it's reachable from any directory:
```bash
claude mcp add --scope user --transport http robinhood-trading https://agent.robinhood.com/mcp/trading
claude   # open an interactive session; complete the Robinhood OAuth when prompted
```
Then prove the headless refresh works:
```bash
bash bin/refresh_once.sh        # should print "✓ snapshot refreshed" and update data/account_snapshot.json
```
If it says "snapshot NOT updated", the MCP isn't authenticated for headless use yet — re-open `claude`,
confirm the robinhood tools are available, and retry. (If your MCP is project-scoped instead of user,
set `AGENTIC_MCP_CWD` to that project dir when calling the script.)

### 4. Bring up the stack
```bash
docker compose -f deploy/docker-compose.prod.yml up -d --build
# verify (local):
curl -u you:YOUR-PASSWORD http://localhost:8088/api/health
```

### 5. Cloudflare Tunnel
```bash
cloudflared tunnel login
cloudflared tunnel create agentic-dashboard
cloudflared tunnel route dns agentic-dashboard agentic.yourdomain.com
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
# edit: tunnel id, credentials-file path, hostname, service: http://localhost:8088
sudo cloudflared service install
```
Open `https://agentic.yourdomain.com` → basic-auth prompt → dashboard. (Optionally add a Cloudflare
Access policy in front for a second factor.)

### 6. Start on boot
```bash
sudo cp deploy/agentic-dashboard.service /etc/systemd/system/
# edit User= and WorkingDirectory= in that file first
sudo systemctl daemon-reload
sudo systemctl enable --now agentic-dashboard
```

### 7. Schedule the twice-daily cycle
```bash
crontab -e
# paste deploy/crontab.example, editing the absolute paths + AGENTIC_COMPOSE_FILE
crontab -l   # confirm
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
  `logs/events.jsonl`.
- **Update:** `git pull && docker compose -f deploy/docker-compose.prod.yml up -d --build`.
- **MCP re-auth (the one recurring chore):** Robinhood OAuth tokens expire. If a scheduled refresh logs
  "snapshot NOT updated", SSH in and run `claude` once to re-authenticate the robinhood MCP. The
  dashboard keeps working on the last snapshot + live prices until then.
- **Change the schedule:** edit the crontab times (they're US/Eastern via `CRON_TZ`).

## Security notes

- Caddy basic-auth gates everything; the live account is never exposed unauthenticated. Add Cloudflare
  Access for defense-in-depth.
- The Anthropic key lives only in `backend/.env` (gitignored, `chmod 600`) — never in an image layer or
  any API response.
- No Robinhood credentials are stored; the MCP OAuth token lives in Claude Code's own config on the host.
- The account is read-only: scans/debates/reports only. Trades remain a deliberate, human-driven action.
- Only Caddy is published, to `127.0.0.1` — the backend and frontend have no host ports.
