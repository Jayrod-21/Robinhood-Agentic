# Infra / Refresh-Bridge Review — Robinhood Agentic Dashboard

**Reviewer:** Independent senior infra/security reviewer (did not author this code).
**Scope:** `bin/pick_ports.sh`, `bin/refresh_daemon.sh`, `bin/up.sh`, `bin/refresh_prompt.md`,
`bin/sync_snapshot.md`, `docker-compose.yml`, `.dockerignore`, `.gitignore`,
`backend/.env.example`. Read-only context: `backend/app/routers/refresh.py`,
`backend/Dockerfile`, `frontend/Dockerfile`.
**Method:** Full static read of the three scripts + compose + manifests; sampled the prompt/markdown
and read-only context. `bash -n` and live execution of `pick_ports.sh` were **denied by the sandbox**
(Bash/Read blocked); findings below are from static analysis only. I flag explicitly where a runtime
check would have added confidence.

---

## Summary verdict

**APPROVE WITH SHOULD-FIX CHANGES.** No BLOCKERs.

The bridge is well-conceived and, critically, **secure by construction at the two highest-risk
points**: the refresh prompt is a fixed file (no external string ever reaches the `claude` argv), and
`--allowedTools` is correctly least-privilege — only two read-only RH pulls plus `Write` and
`Bash(date*)`. No order-placement tool is reachable. There is **no command-injection path** I could
find: every value interpolated into the generated runner is a project-controlled constant or `%q`-quoted.
The wt.exe semicolon gotcha is genuinely mitigated (all logic in a temp runner; wt.exe gets a fixed
`wsl.exe bash <runner>` argv with no `;`/`,`). Secrets hygiene is solid across `.gitignore`,
`.dockerignore`, and `.env.example`.

The SHOULD-FIX items are real but bounded: (1) `pick_ports.sh` has a genuine but small TOCTOU window
that is **partially** closed and acceptable for a single-user localhost dev tool, though one logic gap
(the bind test races against itself, and the range overlaps the ephemeral floor for part of its span);
(2) `refresh_daemon.sh` runs with `set -u` only — no `-e`/`-o pipefail`, and the temp runner is never
cleaned up (file-descriptor/`/tmp` litter, minor); (3) a couple of robustness/quoting nits. None rise
to refuse-approval level for a $100 single-user localhost tool.

---

## Bar checklist

| Area | Item | Status |
|------|------|--------|
| Robust shell | `set -euo pipefail` in `pick_ports.sh` | PASS (line 19) |
| Robust shell | `set -euo pipefail` in `up.sh` | PASS (line 12) |
| Robust shell | `set -euo pipefail` in `refresh_daemon.sh` | **FAIL — only `set -u`** (line 21) |
| Robust shell | Spaces-in-path quoting throughout | PASS (all `"${VAR}"`; runner interpolation safe) |
| Robust shell | No word-splitting / glob bugs | PASS w/ one nit (`seq` word-split is intentional & safe) |
| Robust shell | Traps where needed | PARTIAL (daemon traps INT/TERM; no runner cleanup trap) |
| Robust shell | Idempotency | PASS (`up.sh` pkills prior daemon; `.env.ports` overwritten) |
| pick_ports correctness | `secrets.randbelow` random draw | PASS (line 76) |
| pick_ports correctness | 3 free-checks (bind no-REUSEADDR + ss + docker) | PASS (lines 44–68) |
| pick_ports correctness | up-to-15 retries | PASS (line 74) |
| pick_ports correctness | backend ≠ frontend | PASS (line 95 exclude + line 102 guard) |
| pick_ports correctness | final re-verify | PASS (lines 101–106) |
| pick_ports correctness | failure exits non-zero | PASS (lines 90–98, 103–106) |
| pick_ports correctness | TOCTOU window acceptable | SHOULD-FIX (see F2 — acceptable but documentable gap) |
| pick_ports correctness | range vs ephemeral floor | SHOULD-FIX (range 20000–59999 straddles 32768 floor; see F3) |
| daemon security | command-injection path | PASS — none found (see Detailed F-PRAISE-1) |
| daemon security | `--allowedTools` `%q` quoting injection-safe | PASS (line 97) |
| daemon security | wt.exe semicolon gotcha mitigated | PASS (lines 99–114) |
| daemon security | least-privilege tools (no order, no broad Bash) | PASS (lines 45–50) |
| daemon security | mtime-wait logic correct | PASS w/ NIT (see F6) |
| daemon security | leaves trigger on failure? | PASS — always `rm -f` (line 141); see F7 caveat |
| daemon security | `ALLOW_HEADLESS` default + behavior | PASS (default 0 → visible tab) |
| up.sh | docker daemon check + docker.exe fallback | PASS (lines 19–35) |
| up.sh | port export to compose | PASS (lines 39–43) |
| up.sh | `ANTHROPIC_API_KEY` sourcing from backend/.env | PASS (line 42) |
| up.sh | `chmod a+rwX` on bind-mounts present + correct | PASS (lines 51–52) |
| up.sh | daemon start | PASS (lines 69–73) |
| up.sh | clear messaging | PASS |
| compose | `${BACKEND_PORT}/${FRONTEND_PORT}` substitution | PASS (lines 11, 35, 38) |
| compose | `ANTHROPIC_API_KEY` passthrough (empty-string) | PASS (`:-` default, line 13) |
| compose | volume mounts data/logs rw | PASS (lines 20–21) |
| compose | healthcheck | PASS (lines 22–27) |
| compose | `NEXT_PUBLIC_API_URL` → browser reaches localhost:BACKEND_PORT | PASS (line 38) |
| secrets | `.gitignore` covers .env / backend/.env / .env.ports / snapshot / refresh.request | PASS |
| secrets | `.dockerignore` keeps secrets out of build context | PASS |
| secrets | no secret in committed file or image layer | PASS (see F-SEC note on CORS default) |

---

## Findings

### BLOCKER
*(none)*

### SHOULD-FIX
- **F1** — `refresh_daemon.sh` runs under `set -u` only; missing `-e -o pipefail` against the contract.
- **F2** — `pick_ports.sh`: the bind-test self-TOCTOU + best-effort docker/ss checks leave a real
  (small) window; the final re-verify reduces but does not eliminate it.
- **F3** — `pick_ports.sh` range `20000–59999` overlaps the Linux ephemeral range floor
  (`32768` on most hosts); ~57% of the draw space is ephemeral-eligible, weakening the "biased below
  the floor" comment and slightly raising collision odds with outbound sockets.
- **F4** — `refresh_daemon.sh` never removes the `mktemp` runner script (`/tmp` litter; minor leak).
- **F5** — `up.sh`: backend-health loop does not fail/branch if the backend never becomes healthy; it
  silently proceeds to "Dashboard: …" after 40 tries.

### NIT
- **F6** — `try_headless` waits only 4s for the mtime to advance after `claude` exits; tight but
  documented. Consider deriving from a constant.
- **F7** — On timeout the daemon clears the trigger anyway (line 137/141). Intentional (prevents a stuck
  trigger), but a transient MCP failure silently drops the user's request with no retry.
- **F8** — `docker_published_ports`/`ss_listening_ports` parse via `grep -oE`/`awk` on human-formatted
  output; brittle to format changes (IPv6 `[::]:port`, ranges). The authoritative bind test backs them,
  so this is cosmetic.
- **F9** — `frontend/Dockerfile` `COPY . .` after a `frontend`-scoped `.dockerignore` — confirm the
  frontend `.dockerignore` exists and excludes `.env*`/`node_modules` (out of my scope to read; flag).

### PRAISE
- **P1** — Fixed-prompt-file design + `%q`-quoted least-privilege `--allowedTools` = no injection
  surface and no order tool. This is the single most important thing to get right and it is correct.
- **P2** — wt.exe `;`/`,` gotcha mitigation (all logic in a temp runner, fixed wt argv) is exactly the
  documented correct approach.
- **P3** — `pick_ports.sh` uses `secrets.randbelow` (CSPRNG) and a real socket-bind test with
  `SO_REUSEADDR` **off** — the authoritative free-check, not a heuristic. Final re-verify before commit.
- **P4** — Non-root containers + `chmod a+rwX` on bind mounts (line 51–52) correctly reconciles
  uid-1001 container writes with host-owned dirs.

---

## Detailed findings

### F-PRAISE-1 — No command-injection path (verified) — `refresh_daemon.sh:81-82, 95-114`
Traced every interpolation:
- **Headless (lines 81–82):** `"${cb}" --print "$(cat "${PROMPT_FILE}")" --allowedTools "${ALLOWED_TOOLS[@]}"`.
  The prompt is the command-substituted *contents* of a fixed file passed as a **single quoted argv
  element** — it is data, never re-parsed as shell. `"${ALLOWED_TOOLS[@]}"` array-expands to four
  separate quoted words. No external input reaches argv. Safe.
- **Visible tab (lines 101–109):** the runner heredoc is unquoted (`<<RUNNER`), so the daemon's shell
  expands `${cb}`, `${PROMPT_FILE}`, `${runlog}`, `${MCP_CWD}`, `${allowed_quoted}` while writing the
  file. `${cb}`, `${PROMPT_FILE}`, `${runlog}`, `${MCP_CWD}` are all emitted **inside double quotes** in
  the runner, so the embedded spaces in `3b. Robinhood Agentic` survive intact. `${allowed_quoted}` is
  `printf '%q '` output (line 97) emitted **bare** — exactly correct, because `%q` produces a
  shell-safe token list that must NOT be re-quoted. The `$(cat ...)` inside the runner is escaped
  (`\$(cat ...)`) so it runs in the *runner's* shell at tab time, again as a single quoted argv element.
  No value here is attacker-influenced (all are constants defined at the top of the script). **No
  injection.**

The only way to introduce injection would be to make `ALLOWED_TOOLS`, `PROMPT_FILE`, or `MCP_CWD`
externally controllable — none are. The web endpoint (`refresh.py`) writes a JSON trigger file whose
*contents are never read by the daemon* (the daemon only checks existence and mtime), so even a crafted
trigger body cannot reach the shell. Good design.

### F1 — `set -u` only, missing `-e -o pipefail` — `refresh_daemon.sh:21` (SHOULD-FIX)
The contract requires `set -euo pipefail`. The daemon uses `set -u`. Rationale is understandable — a
long-running watch loop should not die on the first non-zero (`stat` on a missing snapshot, a `timeout`
expiry, a failed `claude`), and indeed several calls are `|| true`-guarded. But `-o pipefail` is free
and would catch a silently-broken `... | tee` in `open_visible_tab`. The safer pattern is to keep the
loop resilient *explicitly* (the functions already return status codes consumed by `process_request ||
true` at line 149) while still enabling `pipefail`:
```bash
set -uo pipefail
```
Leaving `-e` off is defensible for a watch loop, but it should be a **deliberate, commented** choice,
not an omission. Add a one-line comment at line 21 explaining why `-e` is intentionally absent. Net:
add `pipefail`, document the absence of `-e`.

### F2 — pick_ports TOCTOU window — `pick_ports.sh:44-68, 100-106` (SHOULD-FIX, acceptable)
The free-check sequence is: ss-set membership → docker-set membership → **bind test with the socket
immediately closed** (lines 59–65, `finally: s.close()`). The bind test is authoritative *at the
instant it runs*, but the socket is closed before the port is ever handed to compose, so between
`pick_one` returning and `docker compose up` publishing the port, anything on the host could grab it
(classic TOCTOU). The final re-verify (lines 101–106) re-runs all three checks right before writing
`.env.ports`, which **shrinks** the window to the gap between that re-verify and compose binding — but
does not eliminate it.

For a **single-user localhost dev tool** this is acceptable: the realistic racer is another invocation
of this same script or an unrelated service start, both rare. I would **not** block on it. Two cheap
hardenings if you want to close it further:
1. Hold the bound sockets open (don't `close()` in the checker) until compose has bound — not worth the
   plumbing here.
2. Accept the residual race and **document it** in the header comment (it currently claims
   per-candidate verification "actually guarantees freedom" at line 17 — that overstates it; it
   guarantees freedom *at check time*, not at *bind time*). Soften that sentence.

`SS_PORTS`/`DOCKER_PORTS` are also snapshotted once (lines 36, 88) and only `SS_PORTS` is refreshed at
re-verify (line 101); `DOCKER_PORTS` is **not** refreshed before the final guard. A container started
between the initial snapshot and the final guard would be missed by the docker check (the bind test
would still catch a *published* port, so this is belt-and-suspenders, but inconsistent). Refresh
`DOCKER_PORTS` at line 101 too, for symmetry.

### F3 — Port range overlaps ephemeral floor — `pick_ports.sh:16-17, 25-26` (SHOULD-FIX)
The header (line 16) says the range is "biased below the local ephemeral range floor (32768 on this
host)," but the actual range is `20000–59999`. On a typical Linux host
(`/proc/sys/net/ipv4/ip_local_port_range` = `32768 60999`), **27232 of the 40000 candidate ports
(~68%) fall inside the ephemeral range** — the opposite of "biased below." Ports above 32768 can be
transiently held by the kernel for *outbound* connections, which the bind test (inbound `bind` on a
specific port) will correctly reject at check time, but which can be allocated *after* the check
(feeds F2). I could not confirm this host's actual range — Read of `/proc/.../ip_local_port_range` was
sandbox-denied — so treat the 32768 figure as the documented assumption.

Fix: either (a) set `PORT_MAX=32767` to genuinely stay below the conventional floor (still 12768
candidates × 15 draws — plenty), or (b) correct the misleading comment to say the range *overlaps* the
ephemeral range and that the bind test is the sole guarantee. (a) is the stronger choice and cheap.

### F4 — Temp runner never cleaned up — `refresh_daemon.sh:92, 101-114` (SHOULD-FIX, minor)
`runner="$(mktemp /tmp/agentic-refresh-XXXXXX.sh)"` is created on every visible-tab refresh and never
removed. The tab runs asynchronously (`&` + `disown`, lines 114/118), so the daemon cannot safely
`rm` it immediately (the tab may not have `exec`'d yet) — but over a long-lived daemon this leaks one
`/tmp` script per refresh. Two options: have the **runner delete itself** as its last act
(`rm -f -- "$0"` before the `read -p`), or sweep `/tmp/agentic-refresh-*.sh` older than N minutes at the
top of `process_request`. The self-delete is cleanest. Note the runner currently ends with a blocking
`read -p "Press Enter to close..."` (line 108) which holds the tab open — good UX — so self-delete
should come *before* that read so the file is gone even if the user leaves the tab open.

### F5 — Health loop has no failure branch — `up.sh:58-66` (SHOULD-FIX)
The loop tries 40×2s = 80s for `/api/health`, but if the backend never comes up it falls through to
print "Dashboard: …" as if all is well. A user then hits a dead dashboard with no diagnostic. Capture
success and branch:
```bash
healthy=0
for _ in $(seq 1 40); do
  if curl -fsS "http://localhost:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then healthy=1; echo " ✓"; break; fi
  echo -n "."; sleep 2
done
if [ "${healthy}" -ne 1 ]; then
  echo " ✗"
  echo "Backend did not become healthy in 80s. Check: ${DOCKER} compose logs backend" >&2
  exit 1
fi
```
Starting the refresh daemon and printing a URL for an unhealthy stack is misleading.

### F6 — Headless post-exit grace window hard-coded — `refresh_daemon.sh:83` (NIT)
`wait_for_update "${baseline}" 4` allows only 4s for the mtime to settle after `claude --print` exits.
Since `claude` writes the snapshot *before* printing `DONE` and exiting, 4s is generous in practice, but
the magic `4` should be a named constant (e.g. `HEADLESS_GRACE=4`) for clarity. Cosmetic.

### F7 — Trigger cleared on timeout (no retry) — `refresh_daemon.sh:137,141` (NIT/by-design)
On a tab timeout the daemon logs "left trigger cleared anyway" and `rm -f`s the request (line 141 runs
unconditionally). This correctly prevents a stuck trigger from re-firing forever, but it also means a
transient MCP/OAuth hiccup silently swallows the user's refresh with no retry and no error surfaced to
the dashboard (the web layer only learns via the unchanged `generated_at`). Acceptable for v1; consider
writing a `data/refresh.error` breadcrumb the frontend can show, so a failed refresh isn't invisible.

### F8 — Brittle port-list parsing — `pick_ports.sh:30-41` (NIT)
`docker ps … | grep -oE ':[0-9]+->'` and `ss -ltn | awk '{print $4}' | grep -oE '[0-9]+$'` parse
human-formatted output. IPv6 listeners render as `[::]:PORT` (the `awk $4` + `grep '[0-9]+$'` handles
that) and docker port ranges (`:8000-8005->`) would be partly missed. The authoritative bind test
covers any miss, so these are advisory pre-filters only — fine as-is, but worth a comment that they're
best-effort.

### F-SEC — Compose defaults `CORS_ORIGINS=*` — `docker-compose.yml:16`, `.env.example:13` (NOTE)
Default CORS is `*` (any origin). For a localhost-only single-user dev dashboard with no auth and only
read endpoints + a rate-limited refresh trigger, the blast radius is low, but `*` means any website the
user visits can script requests to `localhost:BACKEND_PORT`, including `POST /api/refresh` (spawns a
terminal tab — the cooldown in `refresh.py:66` caps the storm to one tab per cooldown window). Not a
BLOCKER given the threat model, but I'd tighten the default to
`http://localhost:${FRONTEND_PORT}` rather than `*`, especially since the refresh endpoint has a
real-world side effect (opening a window / running `claude`). Out of strict scope (router not in
review list) but noted because the default lives in the in-scope compose/`.env.example`.

### Secrets hygiene — verified clean
- `.gitignore` (lines 9–19) covers `.env`, `backend/.env`, `*.key`, `.env.ports`,
  `data/refresh.request`, `data/refresh.lock`, `data/account_snapshot.json`, `logs/refresh/`. Complete
  against the project's secret surface.
- `.dockerignore` (lines 6–17) excludes `.git/`, `.env`, `.env.ports`, `backend/.env`, `data/`,
  `logs/`, `docs/`, `reference/`, `JMWFM/`. Secrets and runtime artifacts stay out of the backend build
  context. `frontend/` is excluded from the backend context and has its own context + ignore (compose
  line 32) — but I could not read the frontend `.dockerignore` (F9).
- `backend/.env.example` (line 6) ships `ANTHROPIC_API_KEY=` **empty** — no real secret committed. Good.
- No credential, token, or key literal appears in any in-scope file. The Robinhood account number
  `542574025` in `refresh_prompt.md`/`sync_snapshot.md` is an account *identifier*, not a credential,
  and the masked form `••••4025` is what the snapshot exposes — acceptable.

---

## Coordination observations

- **`refresh.py` ↔ daemon contract is clean and decoupled:** the web layer writes a JSON trigger whose
  *body the daemon never reads* (daemon keys only on file existence + snapshot mtime). This is good
  defense-in-depth — a malicious or malformed trigger body cannot influence daemon behavior or reach the
  shell. The cooldown + "pending" check in `refresh.py:59-74` correctly prevents tab storms, complementing
  the daemon's single-threaded poll loop.
- **mtime as the success signal** couples three processes (web, daemon, the `claude` tab) loosely and
  correctly: the daemon's `wait_for_update` and the router's `snapshot_generated_at` both read the same
  snapshot, so "did the refresh work?" has one source of truth. Only gap: F7 — a *failed* refresh is
  invisible to the web layer (no error breadcrumb).
- **`up.sh` → `pick_ports.sh` → compose** port handoff is sound: ports flow file → `set -a; source` →
  compose `${VAR}` substitution → `NEXT_PUBLIC_API_URL` for the browser. The browser correctly targets
  the *host-published* backend port (compose line 38), not the in-container `8000`. Verified consistent.
- **Daemon lifecycle vs `up.sh`:** `up.sh:69` `pkill -f "bin/refresh_daemon.sh"` before restart is
  idempotent, but `pkill -f` matches the *pattern* anywhere in the command line — if a user happened to
  `vim bin/refresh_daemon.sh`, that editor would also match and be killed. Low probability; consider a
  PID file for precision. (NIT, not listed above to keep the count focused.)
- **MCP scope discipline:** running `claude` from `MCP_CWD=/root/Jared` (line 32) so the project-scoped
  RH MCP loads, combined with the `--allowedTools` allowlist, means the refresh process has exactly the
  RH read tools and nothing else — the order-placement tools enumerated in the MCP are *present* in the
  server but *unreachable* without an interactive approval that the headless/`--print` path cannot grant.
  This is the right least-privilege posture and the most reassuring property of the whole bridge.

---

**Bottom line:** ship it after F1–F5. The security core (fixed prompt, `%q` allowlist, no order tool,
wt.exe mitigation, secrets hygiene) is correct; the remaining items are robustness and a documentable
port-range/TOCTOU nuance, none of which can hand out a used port in practice (the bind re-verify
backstops it) or leak a secret.
