# Agentic Dashboard

A containerized, interactive dashboard for the Robinhood "Agentic" account (••••4025) — a sibling of
3a's Wasden Watch UI. It shows the **real** account, runs the live Sprinkle Sauce screen, and runs a
live bull/bear + 10-agent jury debate, all on freshly-picked random ports.

## Run it

Preconditions: **a reachable Docker daemon** — on Linux the `docker` service running with your user
in the `docker` group; under WSL, Docker Desktop with WSL integration enabled. For live debates only,
put a key in `backend/.env` (copy `backend/.env.example`).

The Portfolio page needs an account snapshot, which comes from the host-side `robinhood-trading` MCP
(see "The Refresh bridge" below). On a fresh machine that MCP has to be added and
OAuth-authenticated once before `/api/account` returns anything but a 503:

```bash
claude mcp add --scope user --transport http robinhood-trading <your-MCP-URL>
claude   # interactive session; complete the Robinhood OAuth when prompted
```

```bash
bash bin/up.sh
```

This picks two fresh free ports, builds both images, starts the stack, starts the host-side refresh
daemon, and prints the URLs. Open the **Dashboard** URL it prints (e.g. `http://localhost:42010`).

Stop:

```bash
docker compose down
pkill -f bin/refresh_daemon.sh
```

## Pages

- **Portfolio** — real positions and cost basis from the Robinhood snapshot, with live prices/P&L
  from yfinance, an allocation donut, and a **Refresh from Robinhood** button (see below). Read-only.
- **Scan** — runs the real Sprinkle Sauce screen over the seed universe (or tickers you type),
  streaming each result and ending with a ranked survivor list. No LLM, no cost.
- **Pipeline** — one ticker through the full chain (screen → bull → bear → jury → decision) as a live
  node stepper.
- **Debate** — a bull and bear build the cases, 10 jurors vote, and the result aggregates: 6+ decides,
  a 5-5 tie escalates to you. Past debates (including the hand-written archives) are listed.

## The Refresh bridge (how "connected" works)

The `robinhood-trading` MCP is an OAuth server scoped to the host Claude session, so a Docker
container can't reach it. The Refresh button instead:

1. `POST /api/refresh` → the backend drops `data/refresh.request` on the shared volume.
2. `bin/refresh_daemon.sh` (running on the host, started by `up.sh`) sees the trigger and opens a
   Windows Terminal tab running `claude`, which connects the Robinhood MCP, pulls the account, and
   rewrites `data/account_snapshot.json`.
3. The dashboard polls `/api/account` and flips to "updated just now".

No Robinhood credentials are stored anywhere. Live prices/P&L refresh on their own via yfinance; only
the holdings re-pull goes through this bridge. If the daemon isn't running, see `bin/sync_snapshot.md`
for the manual one-liner.

## Enabling live debates

The account and scan pages work with no key. The Debate and Pipeline pages call the Anthropic API
(each debate spends tokens), so they need a key:

```bash
cp backend/.env.example backend/.env
# edit backend/.env: set ANTHROPIC_API_KEY=...
bash bin/up.sh   # re-reads the key
```

Jurors default to the cheap `claude-haiku-4-5`; bull/bear synthesis uses `claude-sonnet-4-6`. Both are
configurable in `backend/.env`. The debate endpoint is rate-limited.

## Limitations

- Read-only account: no order placement from the browser (trades stay with the in-session agent).
- The Refresh button needs the host daemon running; the container can't launch host terminals.
- Live debates cost Anthropic tokens.
