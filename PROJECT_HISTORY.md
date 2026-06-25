# Project History — 3b. Robinhood Agentic Trader

## 2026-06-03 — Project bootstrapped
- Spun off from `3a. SpecialSprinkleSauce` as a separate folder/repo to keep the live trading
  journal and personal aggressive strategy out of the shared Wasden Watch repo.
- Established baseline: Robinhood "Agentic" account ••••4025, $100.00 all cash, zero positions.
- Verified PDT rule repeal (effective 2026-06-04) via web search.
- Authored operating charter (`docs/AGENTIC_ROBINHOOD_v1.md`) and trading journal scaffold.
- Resolved key decisions: universe = large-cap + liquid volatile mid/small-caps; cadence = daily
  pre-market scan (review ≠ forced trade); fundamentals data = yfinance; first trades = bootstrap
  (Jared confirms every order).
- Architecture: in-session Claude agent IS the intelligence layer (no paid LLM API tokens);
  Robinhood MCP for quotes + execution; references 3a for the Wasden framework.
- Built the daily-scan (increment 1): `src/screen.py` (lean Sprinkle Sauce tiers, pure/testable),
  `src/data.py` (yfinance adapter, fails soft per ticker), `src/universe.py` (curated seed),
  `src/daily_scan.py` (runner + report). 18 unit tests pass; live smoke scan works
  (OXY survived; AAPL rejected on PEG, NVDA on thin FCF yield). Registered in `TESTS.md`.
- Next: risk/sizing + pre-trade validation for $100, then Robinhood execution glue
  (review → confirm → place → journal).

## 2026-06-16 — Dockerized live dashboard + debate engine
- Built a containerized dashboard (sibling of 3a's Wasden Watch UI) the user can interact with:
  - **Backend** (`backend/`, FastAPI): `/api/account` overlays the real Robinhood snapshot with live
    yfinance prices (P&L, weights, allocation); `/api/scan/run-stream` streams the existing `src`
    Sprinkle Sauce screen; `/api/debate/run-stream` + `/api/pipeline/run-stream` run a live
    bull/bear + 10-agent jury (Anthropic API) with 3a's consensus rules (6+ decides, 5-5 escalates);
    `/api/refresh` queues an account re-pull. Reuses `src/` unchanged.
  - **Frontend** (`frontend/`, Next.js 14 + Tailwind + Recharts): Portfolio, Scan, Pipeline, and
    Debate pages, plus a Refresh button. Dark trading aesthetic.
  - **Refresh bridge** (the user's idea): the button drops `data/refresh.request`; a host-side
    daemon (`bin/refresh_daemon.sh`, outside Docker) pops a `claude` terminal tab that connects the
    Robinhood MCP, rewrites the snapshot, and reports back. No stored credentials — the MCP is
    OAuth-scoped to the host session. Verified the headless `claude --print … --allowedTools` path
    can reach the MCP as root (the `--dangerously-skip-permissions` flag is root-blocked).
  - **Ports:** `bin/pick_ports.sh` draws up to 15 random ports, each verified free three ways
    (socket bind + `ss` + `docker ps`); picks backend then frontend independently.
- Decisions (asked up front): bridge-snapshot connection (no stored creds), live LLM debate engine,
  two-service compose mirroring 3a.
- Verified end-to-end in Docker: real account data through the container ($196–198, live P&L),
  scan SSE, debate-record archive parsing, refresh trigger. 16 backend + 18 src tests pass; frontend
  builds clean. **Live debates need `ANTHROPIC_API_KEY` in `backend/.env`** (precondition).
- Gotchas fixed: bind-mounted `data/`/`logs/` must be writable by the non-root container user
  (`up.sh` chmods them); an empty `${ANTHROPIC_API_KEY:-}` from compose is now coerced to `None`
  so readiness reporting is honest.
- Hardened via a full `/fixpass` cycle (4 independent reviewers → fix-pass → re-review, PASS):
  fixed a path-traversal in `get_record`, added a shared rate limiter for the debate+pipeline
  endpoints, capped scan ticker lists, de-wildcarded CORS, and fixed a frontend error state. Tests
  16 → 66.
- Local dev environment: `.venv` + `Makefile` (`make up/down/test/lint/logs/debate/scan/account`,
  `make dev-backend`/`dev-frontend`).

## 2026-06-16 (cont.) — Ubuntu server deployment + twice-daily cycle
- Goal: run on a home Ubuntu server, 24/7, triggered at market open + close. Decisions: headless
  Claude+MCP for the snapshot refresh (no stored creds), the cycle does refresh+scan+debate+report,
  public via Cloudflare Tunnel behind basic auth.
- Built: `bin/refresh_once.sh` (headless MCP pull, replaces the WSL-only wt.exe bridge on a server;
  `refresh_daemon.sh` now auto-detects no-wt.exe → headless); `app/jobs/cycle.py`
  (`python -m app.jobs.cycle open|close` — scan + per-position debates + `logs/reports/`);
  `bin/scheduled_cycle.sh` (host refresh → `docker compose exec` the cycle); `deploy/` topology
  (Caddy single-origin + basic auth, `Dockerfile.prod` real Next build, `docker-compose.prod.yml`,
  `crontab.example`, `agentic-dashboard.service`, `cloudflared-config.example.yml`); `SERVER_DEPLOY.md`.
- Verified locally: headless `refresh_once.sh` refreshed the real snapshot; the cycle ran end-to-end
  (real account + 7 scan survivors + a live debate → report); the full prod stack smoke-passed
  (no-auth → 401, auth → 200, same-origin `/api/account` real data through Caddy). 69 backend + 18
  src tests pass.
- Known recurring chore: the Robinhood MCP OAuth token expires; re-auth over SSH (`claude`) when a
  scheduled refresh logs "snapshot NOT updated". Dashboard keeps serving last snapshot + live prices.
