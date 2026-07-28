# 3b — Project Plan

**Status as of 2026-07-27.** This is the living plan: what is broken right now, what gets built next
and in what order, how each phase is tested, and how the work is tracked. `PROJECT.md` says what the
project *is*; this says what happens next. Update it as phases land.

The destination is stated in one line: **do what 3a (Sprinkle Sauce / Wasden Watch) does — the same
brains, on better data — but live on my own Robinhood account, hosted on M.**

---

## 1. Where the project actually is

Built and working: the Sprinkle Sauce screen on yfinance, the forward-thesis layer, Robinhood
execution through the MCP, the append-only `logs/` archive, a Dockerized dashboard (FastAPI +
Next.js) with a 10-agent jury debate engine, the host-side refresh bridge, and a prod deploy
topology with a twice-daily cron cycle.

Verified live on M on 2026-07-27: `.venv` installs clean, 87 tests pass, the frontend builds, the
Docker stack comes up on random ports, all four pages serve, and both the container scan endpoint
and the `src.daily_scan` CLI return real yfinance fundamentals.

What that verification did **not** cover, because it can't yet: anything touching the live account.

---

## 2. Current problems

Ordered by what blocks the most downstream work. IDs are stable — reference them in commits and
issues.

### P1 — The `robinhood-trading` MCP needs its OAuth completed  🔴 blocking

Every live-account path depends on it: the Portfolio page, the Refresh button, `bin/refresh_once.sh`,
and the twice-daily cycle job. Without it `/api/account` returns 503 and `/api/health` reports
`snapshot_present: false`.

**Registered 2026-07-27** at user scope — `https://agent.robinhood.com/mcp/trading`. `claude mcp list`
now reports it as `! Needs authentication`. The remaining step is interactive and cannot be scripted:
run `claude` on M and complete the Robinhood OAuth when prompted.

Two related bugs found while wiring this up, both now fixed: `bin/refresh_daemon.sh` hard-coded
`MCP_CWD="/root/Jared"` — the projects root on the retired WSL box — so every headless refresh `cd`'d
into a nonexistent path and exited 1, which is indistinguishable from "the MCP isn't authenticated".
It now defaults to the project root. And `SERVER_DEPLOY.md` recorded only a `...` placeholder for the
URL, which is why it was unrecoverable; the real URL is now in the docs.

### P2 — The live slate has run unmonitored for ~7 weeks  🔴 real money

Seven positions (TSM, VST, NVDA, V, CVX, GEV, QCOM) were deployed 2026-06-03. The last cycle report
is `logs/reports/2026-06-16-close.md`. There is no crontab on M, so nothing has refreshed, debated,
or checked a stop since. Blocked on P1. Nobody knows the current P&L — and the charter's discipline
rules (stops, exit-before-entry, cash floor) have had no enforcement in that window.

### P3 — No position monitoring or stop discipline  🟠

The dashboard reports state; it does not *watch* it. There are no stop levels, no target levels, no
drift-from-target alerts, and no alerting path at all. This is the unchecked roadmap item in
`PROJECT.md`, and it is what would have made P2 harmless.

### P4 — No outcome logging or lesson capture  🟠

Closing a position writes nothing back. The journal is the "self-learning substrate" in name, but
there is no structured record of *entry thesis → exit → what actually happened → what we learned*.
Every debate is therefore reasoning from scratch rather than from a track record.

### P5 — yfinance is the only data source  🟠

Unofficial, rate-limited, and schema-unstable. It is also the reason the screen is thin: the
Piotroski inputs and several Wasden signals are approximated from whatever `.info` happens to
return. FMP replaces it (§4, Phase B).

### P6 — No local database  🟠

Everything is JSON files and markdown. That is fine for a journal and fatal for backtesting,
time-series portfolio value, and any ML. There is no price history, no fundamentals history, and no
positions history — so `BACKLOG.md` item 5 (the portfolio value chart) has nothing to plot.

### P7 — Deploy is documented for a server that no longer exists  🟠

`SERVER_DEPLOY.md` and `deploy/` target a generic Ubuntu box with Caddy + basic auth. The actual
target is now M behind a Cloudflare Tunnel on a `jaredstudio.com` subdomain, matching 9b. Basic auth
in front of a live brokerage view is also weaker than what 9b already does for a language app.

### P8 — No security threat model  🟡

There is no `SECURITY.md` anywhere in the repo. 9b has six. For an app that fronts a live brokerage
account, a snapshot of real holdings, an Anthropic API key, and a side-effecting refresh endpoint —
and that is about to gain an order-placement path (`BACKLOG.md` item 2) — that gap should close
before it goes on the public internet, not after.

### P9 — Host tooling gaps on M  🟡

`make` is not installed even though the Makefile is the documented entry point. Host Python is 3.14
while containers are 3.12, so "passes locally" and "passes in CI" are not the same statement; 9b
solved this by running every suite in a pinned container. No `ANTHROPIC_API_KEY` in `backend/.env`,
so debates and pipeline 503.

### P11 — Fourteen concrete security findings from the 2026-07-27 review  🔴 partly fixed

An adversarial review against 9b's threat models turned up 14 verified findings, recorded in
`docs/SECURITY_FINDINGS_2026-07-27.md` and each confirmed against the source. **Three were live
exposures and are fixed** in the same commit that recorded them:

- **F2 (fixed)** — `bin/up.sh` ran `chmod -R a+rwX` on `data/` and `logs/`, leaving them `0777` and
  `logs/events.jsonl` `0666`. That made the holdings snapshot world-readable *and* let any local
  account forge `data/refresh.request`, which the daemon acts on purely because it exists — bypassing
  the API cooldown entirely. Root cause was a uid mismatch (container 1001 vs host 1000); fixed by
  running the container as the host user and setting the directories `0700`.
- **F3 (fixed)** — `docker-compose.yml` published both services on `0.0.0.0`. The dev stack has no
  auth in front of it, so anything on the LAN could read `/api/account` and POST `/api/refresh`. Now
  bound to `127.0.0.1`.
- **F14 (fixed)** — the stale `MCP_CWD` described under P1.

**Still open**, tracked as issues: F1 CSRF on `POST /api/refresh` (no `Origin`/`Sec-Fetch-Site` check
anywhere, and HTTP Basic has no `SameSite` equivalent — this becomes forced order placement the day
the Commit button ships); F4 no rate limit on `/api/scan/run-stream` while the adjacent paid
endpoints share a limiter; F5 upstream exception text streamed verbatim to clients; F6 held tickers
logged at INFO; F7 no log redaction filter; F8 fully unpinned dependencies with no vulnerability
scanning; F9 no container hardening in either compose; F10 no security response headers; F11 Caddy
basic-auth with no lockout and a default `admin` username; F12 `TICKER_RE` accepts `A..`; F13 the
snapshot schema permits negative quantities and never checks `schema_version`.

The recommendation on auth is to replace basic-auth-only with the same shape 9b runs — Argon2id, an
opaque `SameSite=Strict` session cookie, and mandatory TOTP — keeping Caddy basic-auth as an outer
gate. The asset being protected is not the ~$200 balance; it is order-placement authority against a
real brokerage account, plus a host-side Claude session already OAuth'd to that broker.

### P10 — The global pre-push hook false-fails on M  🟡

It runs pytest in the system interpreter without the project venv, so it blocks `git push` with
dependency errors. Not a git hook, so `--no-verify` does not skip it.

---

**The learning signal:** `docs/EVALUATION_FRAMEWORK.md` specifies how the system scores a decision —
Sharpe + Sortino as a risk-adjusted reward, applied at three points (post-trade feedback, judge
self-review, per-persona counterfactual track records) plus a blind control agent. It drives the
Phase 2 schema and is the definition of "quantifiable" for Phase 7.

**The data on hand:** `docs/DATA_INVENTORY.md` — 4.5 GB of Polygon minute bars and Bloomberg
fundamentals in `data/market/` (gitignored). Enough to build and validate the pipeline; not yet
enough to trust an edge estimate.

**Prior art:** `docs/PATTERNS_FROM_9B.md` surveys 9b Korean Master — which runs this same shape on M
already — and records which of its deploy, database, test-gate, and security patterns to port, with
the reasoning behind each. Phases 1, 2, and 6 below lean on it directly.

---

## 3. Guiding constraints

These are decisions already made. They bound every phase below.

| Constraint | Detail |
|---|---|
| **Solo and private** | One user, one account. No team governance, no multi-user auth, no public signup. Drop 3a's override/jury/approval UI. |
| **Local-first** | Postgres in Docker on M. No Supabase, no cloud DB, no paid cloud infra. |
| **FMP free tier for now** | **250 API calls per day.** Cache aggressively, fixture-back every test, never loop the universe against live FMP. Paid license (~300 calls/min) comes once the build is ready to use it. |
| **Guardrails must not block valid trades** | Jared lost ~$4k to mis-set guardrails. Every risk rule must be tunable, observable, and overridable — never a silent block. A guardrail that fires must say which rule, on what input, and how to override. |
| **Hosting = M + Cloudflare Tunnel + `jaredstudio.com`** | Same shape as 9b. Named tunnel, not a Quick Tunnel. Subdomain TBD. |
| **Zero-downtime deploys — "purple/yellow"** | 3b runs a live UI that Jared interacts with, so redeploys must not drop it. Port 9b's two-color machinery under **purple/yellow** naming (distinct from 9b's blue/green): deploy-inactive → validate → switch, with a split-brain gate and auto-rollback on failed post-switch health. |
| **UI correctness comes before the debate engine** | The 10-agent jury is deprioritized — it spends Anthropic tokens and is not what's blocking. Get the dashboard and the non-debate paths right first. |
| **Verify a host port is free before creating a container** | M hosts several live stacks at once (9b holds 1840–1843 plus its DB). On a collision, pick a **new** port — never take over the one in use. `bin/pick_ports.sh` already does this correctly and is the reference: socket bind + `ss` + `docker ps`. |
| **`SENIOR_ENGINEER_BAR.md`** | The canonical quality contract at the projects root, symlinked into this repo. **§7.2 covers live-money algorithmic trading** and is binding here: `Decimal` never float for money, kill switch, unique client order id, reconcile with the broker before any resubmit, no-lookahead backtests. |
| **Robinhood's API keeps growing** | Re-check what the MCP exposes before designing around a limitation. A capability that was missing in June may exist now. |
| **/fixpass on everything created** | Every feature: build → tests → `/fixpass` (independent reviewers → aggregate → independent fix-pass agent → independent re-review agent) → then merge. All four stages, every time, per feature group — never batched at the end, never shortcut. |

---

## 3a. The gate on spending money (restated 2026-07-28)

Two workstreams must reach a polished state **before** the FMP Premium purchase and before any live
testing resumes. Strategy tuning is explicitly deferred until then.

**Workstream A — infrastructure the debate engine can actually reason over.** The agents must be able
to read the **knowledge base and the database** and do real analysis, rather than reasoning from a
prompt plus a live yfinance call. Today `backend/app/debate/` fetches yfinance at request time and has
no database access and no knowledge-base retrieval at all. Closing that means: DB-backed context
(fundamentals with their `known_at`, price history, prior decisions and the realized Sharpe/Sortino
they earned), plus the knowledge-base read path that makes §3.2 and §3.3 of
`docs/EVALUATION_FRAMEWORK.md` possible — a judge cannot review the outcome of its own prior judgment
if nothing stores that outcome where it can read it.

**Workstream B — the UI.** A localhost prototype exists (Portfolio / Scan / Pipeline / Debate). It
needs features added and its flaws fixed so it is the thing Jared actually interacts with: talking to
the agents, seeing the dilemmas, and reading the evidence behind a decision. First-class deliverable,
not a viewer over the API. `BACKLOG.md` items 1, 3, 4, 5 and issue #21 (the Weight column reporting a
share of equity while the charter's limit is a share of account value) belong here.

**Then, and only then:** buy FMP Premium, load real fundamentals history, and begin testing.

Note what this ordering buys: every phase below can be built and validated against the five years of
minute bars already on disk, so the purchase happens when there is something ready to consume it
rather than a subscription ticking while the pipeline is still being written.

---

## 4. Build phases

Sequenced so each phase is independently useful and testable, and so nothing depends on a phase
that hasn't landed. **Phase 0 is not optional** — everything after it either touches money or
depends on data that doesn't exist yet.

### Phase 0 — Restore live operation and make it safe to leave running

*Closes P1, P2, P9, P10. Prerequisite for everything else.*

1. Re-add and authenticate the `robinhood-trading` MCP at user scope (§6).
2. Refresh the snapshot; run `python -m app.jobs.cycle close` for a full account + scan + debate
   report. **Find out where the seven positions actually stand.**
3. Review each position against its thesis in `docs/THESES.md`. Anything whose thesis has broken
   gets an explicit decision, journaled.
4. Install the twice-daily cron cycle on M so the gap cannot silently reopen.
5. Host tooling: `sudo apt install make`; `ANTHROPIC_API_KEY` into `backend/.env`; fix the pre-push
   gate so it uses the project venv.

**Done when:** the Portfolio page shows the real account, a cycle report exists for today, and cron
is installed and has fired at least once.

### Phase 1 — Security baseline

*Closes P8. Comes before public exposure, not after.*

Port 9b's per-surface `SECURITY.md` model: one at `deploy/` for the hosting surface, one at
`backend/` for the app surface. Enumerate **attack vector → defense** for authentication, the
refresh endpoint (it has side effects and is currently reachable by anyone who can reach the
backend), the account snapshot at rest, API key handling, CORS, rate limiting, dependency
vulnerabilities, and logging. Add a `.gitleaks.toml` with `[extend] useDefault = true` — the
gitleaks workflow already exists, but a future custom config without that line would silently
disable all scanning.

**Done when:** both SECURITY.md files exist with every listed surface covered, and each stated
defense is either verified in code or filed as an issue.

### Phase 2 — Data foundation: FMP + local Postgres

*Closes P5, P6. The structural lift the rest of the platform stands on.*

- A **provider interface** with two implementations: the existing yfinance path and a new FMP path.
  This matters — it keeps yfinance usable as a free fallback and lets the 250/day tier be swapped
  for the paid one without a rewrite.
- **Postgres in Docker on M**, loopback-bound, on an internal network with no egress (9b's posture).
  Schema: securities, fundamentals snapshots (dated), price history, positions history, portfolio
  value points, and scan/debate run records.
- A **caching layer that treats API calls as the scarce resource they are.** Fundamentals get
  fetched once per day per ticker and read from Postgres thereafter. Every test is fixture-backed;
  a live-FMP test is opt-in behind an explicit flag.
- Backfill price history from yfinance (free, bulk) so backtesting has something to run on from day
  one rather than accumulating forward.

**Done when:** a scan runs entirely from Postgres with zero live API calls on a warm cache, and the
daily call budget is logged and enforced with a hard stop.

### Phase 3 — Brains: port 3a's screening engine

*Deepens the screen. Depends on Phase 2 for the inputs it needs.*

Adapt (not copy) 3a's `screening_engine.py` and `piotroski.py`. FMP supplies the full statements a
real Piotroski score needs, so the approximations in `src/screen.py` can become the actual metric.
Keep 3b's leaner shape; drop 3a's team-governance coupling.

**Done when:** the ported screen reproduces 3a's ranking on a shared set of tickers, with any
divergence explained.

### Phase 4 — Backtesting

*Depends on Phase 2's price history and Phase 3's screen.*

Port 3a's `backtesting/`. The first real question to answer: **does the Sprinkle Sauce screen
actually beat buy-and-hold SPY over the backtest window, after costs?** Point-in-time correctness is
the thing to get right — a backtest that screens on today's fundamentals against past prices is
lookahead bias and will lie confidently.

**Done when:** a backtest runs over ≥5 years, reports return/drawdown/Sharpe against SPY and QQQ,
and has an explicit test proving no lookahead.

### Phase 5 — Guardrails and position monitoring

*Closes P3, P4. Where the charter's risk rules become code.*

- Encode the charter rules: ≤25% per name, 10–20% cash floor, exit-before-entry, no averaging down.
- Per-position stops and targets, tracked and evaluated each cycle.
- **Every guardrail is tunable, observable, and overridable** — config-driven thresholds, a log line
  naming the rule and the input whenever one fires, and a documented override. This is the ~$4k
  lesson; a silent block is a bug, not a safety feature.
- Outcome logging on close: entry thesis → exit reason → realized P&L → lesson, written back to the
  journal and the DB so future debates can read the track record.

**Done when:** a simulated position breaching each rule produces the right decision *and* a legible
explanation, and a closed position writes a complete outcome record.

### Phase 6 — Hosting on M

*Closes P7. Depends on Phase 1.*

Rework `deploy/` from the generic-Ubuntu + Caddy + basic-auth shape to 9b's: named Cloudflare Tunnel
to a `jaredstudio.com` subdomain, only the LB binding a non-loopback port, everything else
loopback-only or no host port, no public DNS A record at the host IP, `X-Forwarded-Proto https`
since TLS terminates at the edge. Port 9b's `local-test.sh` idea — run every suite in a container
pinned to the container toolchain (python:3.12), so M's host 3.14 can't produce a false pass.

**Done when:** the dashboard is reachable at its subdomain through the tunnel, the origin is not
reachable directly, and the local test gate runs in pinned containers.

### Phase 7 — Monitoring, alerting, ML

*The long tail. Deliberately last.*

Alerting on stop breaches and cycle failures. The dashboard backlog features (`BACKLOG.md` 1, 3, 4,
5 — the portfolio value chart becomes easy once Phase 2 stores value points). Then the auto-tuner /
ML experiments, which need Phase 4's backtest as their fitness function to be anything but guessing.

**`BACKLOG.md` item 2 — the Commit button — is deliberately unscheduled.** It crosses the app's
read-only boundary and spends real money. It should land after Phase 5's guardrails exist to
constrain it, and it gets its own `/fixpass`.

---

## 5. Testing strategy

The gate that already exists, and where it needs to grow.

**Today** (`TESTS.md`, all green on M): ruff lint pinned via `pyproject.toml`; 18 screen tests; 69
backend tests; frontend production build. CI runs all four on push and PR, with ruff pinned to
0.16.0 so a new ruff release cannot redden an unchanged branch.

**Per-phase additions:**

| Phase | What gets tested, and the trap to avoid |
|---|---|
| 0 | Cycle job end-to-end against the real MCP. Assert the snapshot's mtime actually advanced — "the command exited 0" is not evidence it refreshed. |
| 1 | A test per claimed defense. A security doc whose claims aren't tested is a wish list. |
| 2 | Provider-interface conformance run against **both** implementations. Fixture-backed by default; live-FMP opt-in behind a flag. An explicit test that a warm cache issues zero API calls. |
| 3 | Golden-file comparison against 3a's output on shared tickers. **Use real corpus data, not synthetic placeholders** — a screen that passes on hand-made fundamentals and breaks on a real `.info` payload is the failure mode that already bit 9b. |
| 4 | A dedicated lookahead-bias test. This is the one test that decides whether the backtest is worth anything. |
| 5 | Table-driven cases per rule, asserting both the decision **and** the explanation. Plus the inverse: a valid trade must not be blocked — that regression is the ~$4k lesson in test form. |
| 6 | Suites in containers pinned to the deploy toolchain. Post-deploy: tunnel reachable, origin *not* directly reachable. |

**Standing rules.** Run `/fixpass` after each phase — all four stages, no shortcuts. For any
schema-touching or cross-cutting change, the reviewers and the re-review run the **full** suite, not
the changed slice. Keep `TESTS.md` current as suites are added, since `/testcheck` reads it.

---

## 6. What needs Jared

1. **The `robinhood-trading` MCP URL.** Everything in Phase 0 waits on this. Then:
   ```bash
   claude mcp add --scope user --transport http robinhood-trading <URL>
   claude    # complete the Robinhood OAuth when prompted
   ```
   User scope matters — it makes the MCP reachable from any working directory, which the headless
   `bin/refresh_once.sh` needs.
2. **`sudo apt install make`** — the Makefile is the documented dev entry point.
3. **`ANTHROPIC_API_KEY`** into `backend/.env` (copy `backend/.env.example`) to enable debates.
4. **FMP API key** — not needed until Phase 2. Free tier is fine to start; 250 calls/day.
5. **Subdomain choice** for the `jaredstudio.com` tunnel — not needed until Phase 6.

Recurring chore: the Robinhood MCP OAuth token expires. When a scheduled refresh logs "snapshot NOT
updated", re-auth by running `claude` on M. The dashboard keeps serving the last snapshot with live
prices in the meantime, so this degrades rather than breaks.

---

## 7. Tracking

Repo: `Jayrod-21/Robinhood-Agentic` (private, SSH).

- **Phases** become GitHub milestones; **problems P1–P10** become issues, labeled and linked to the
  phase that closes them.
- Work lands on a branch and merges via PR, so CI runs before anything reaches `master` — the same
  discipline used on 3a and the Stats Website.
- `/fixpass` paper trails go in `docs/fixpass/`, following 9b's convention:
  `BUILD_<feature>.md` → `REVIEW_<area>.md` → `FIX_REPORT_<feature>.md` → `REVIEW_FIXES_<feature>.md`.
- This document is the index. When a phase completes, update §2 and §4 rather than letting the plan
  drift out of date.
