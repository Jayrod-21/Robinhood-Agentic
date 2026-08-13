# Patterns worth porting from 9b (Korean Master)

9b runs the same shape 3b is heading for: a multi-container stack on M, a local Postgres, a
Cloudflare Tunnel on a `jaredstudio.com` subdomain, and a self-hosted test-and-deploy pipeline. It
has been through many `/fixpass` cycles, so its scripts encode a lot of paid-for lessons. This is a
survey of what to reuse and — more usefully — **why each thing is shaped the way it is**, so we
port the reasoning rather than cargo-culting the file.

Source: `~/projects/9b. Korean Master/`. Read at 2026-07-27.

---

## 1. Deploy topology and the standup / teardown flow

9b's pipeline order, stated in its own `TESTS.md`:

> **test → build → smoke → stand up → validate → switch to prod**

Four scripts, each with one job:

| Script | Role |
|---|---|
| `Deploy/local-test.sh` | The authoritative gate. Must pass before anything is built. |
| `Deploy/local-build.sh [TAG]` | Builds images into the **local Docker image store** — no registry, no tar artifacts. Touches nothing running. |
| `Deploy/local-standup.sh` | **Cold** first boot only, for when no color is serving yet. Idempotent. |
| `Deploy/azure-deploy-inactive.sh` → `azure-switch-production.sh` | Steady state: stage the new version on the idle color, validate it, then flip. |

Two things worth stealing outright:

**Separating cold-boot from steady-state deploy.** `local-standup.sh` exists precisely because the
zero-downtime path (deploy to the *inactive* color while the *active* one keeps serving) has no
meaning when nothing is serving yet. Conflating those two cases is how you get a "deploy" script
that only works on a machine that's already deployed.

**`rebuild-environment.sh` as an explicit emergency path**, with the cost stated in the header in
capitals: *THIS CAUSES A 1-2 MINUTE PRODUCTION INTERRUPTION*. It also never passes `-v` to
`compose down`, so data volumes survive the bounce. Having a documented break-glass procedure that
says what it will cost you is better than improvising one at 2am.

**Health checking is its own script.** `bg-health.sh` probes every endpoint and prints PASS/FAIL per
target, but exits non-zero *only* for release-critical paths — loopback debug ports are
informational, and a color that isn't running simply isn't probed. Worth copying: a health check
that fails on things that don't matter gets ignored.

> **Note for 3b:** blue/green is almost certainly overkill here. 3b has few concurrent users; a
> few seconds of downtime on a restart costs nothing. Port the *ordering* and the
> *cold-boot/steady-state split*, not the two-color machinery.

---

## 2. Docker compose structure

The part that generalizes is the **network and volume posture**, which is also the security posture:

- **`km-internal`** — a bridge created with `--internal`, so it has **no egress**. The database and
  the internal services live here. A compromised container on this network cannot call out.
- **`km-edge`** — a normal bridge, for the LB ↔ app hop and the app's outbound API calls.
- **`services_default`** — the cloudflared stack's own network, attached so the LB can receive
  tunnel ingress.

Port bindings follow from that: **only the load balancer binds a non-loopback port.** Postgres binds
`127.0.0.1` only. The app containers bind **no host port at all** and are reachable solely over the
internal networks.

**The single-creator rule.** All three compose projects declare the shared networks and volumes as
`external: true`, and one script — `ensure-shared-volume.sh` — is their only creator, using
`inspect || create`. The header explains why: `external: true` means "attach, do not create", so on
a cold box compose would error `network not found`; but letting two compose projects both create it
produces the *"network exists but was not created by compose / incorrect label"* race. One
idempotent creator, run first, resolves both.

Volumes are **name-pinned** (`name: km_db_data`) so the real Docker object isn't
`km-shared_km_db_data` and every project attaches to the same one. Backups deliberately use a host
**bind** mount instead of a named volume, so host scripts and containers address the same files.

---

## 3. The database harness — the most directly reusable piece

3b Phase 2 needs exactly this. 9b's `db/` contains a hand-written migration runner
(`db/migrate.py`, ~300 lines), 158 migration files as `NNN_<name>.{up,down}.sql`, a testcontainers
test suite, ADRs, and a SECURITY.md.

**Why hand-rolled:** Alembic was rejected because it presumes SQLAlchemy models, and 9b owns raw SQL
on purpose. Flyway drags in a JVM, Sqitch a Perl runtime. The bar was: lives in the repo, auditable
in ~300 lines, works with the test harness. That reasoning applies to 3b unchanged.

The guarantees are the valuable part:

- Each migration runs in **one transaction**, and the `schema_migrations` bookkeeping row is written
  **in that same transaction** — partial application is impossible.
- **The runner owns the transaction.** Enforced by the server, not by reading SQL: after a body
  executes and before the bookkeeping row is written, the runner asserts libpq still reports the
  transaction it opened (`transaction_status` INTRANS **and** an unchanged
  `pg_current_xact_id()`) — a stray `COMMIT`/`ROLLBACK`, or even `COMMIT; BEGIN;`, is detected and
  the migration is never recorded. (3b originally rejected transaction keywords by scanning the
  SQL text; three verification rounds forged that scanner, see ADR-002.)
- **Checksums.** Re-running a migration whose file changed since it was applied raises
  `ChecksumMismatch` rather than silently diverging.
- **A destructive gate.** `--allow-destructive` is required for any destructive migration, and
  destructiveness is declared **in the filename** (`NNN_name.destructive.up.sql` — ADR-002): a
  filename cannot be influenced by anything inside the file, so the *classification* cannot be
  forged from contents. A keyword sniff refuses `DROP TABLE`/`TRUNCATE`/etc. in any UNMARKED file
  (even with SQL comments between the keywords), where a false positive costs only a rename — but
  the sniff is best-effort, not a guarantee: it does not cover mass `DELETE FROM`, `DROP COLUMN`,
  or dynamically built SQL (`EXECUTE 'DR'||'OP TABLE …'`), which no text rule can decide. The
  author marking the filename correctly is the real control; the sniff only reduces the cost of
  forgetting. (3b's original content-classified gate — a `-- migrate:` directive read out of the
  SQL — was forged thirteen ways across three reviews; don't rebuild it.)
- `--dry-run` applies nothing (it still creates the `schema_migrations` bookkeeping table if
  absent) but **does** evaluate the destructive gate, so a deploy aborts at the dry-run step
  rather than mid-apply.
- Migration sessions set `statement_timeout = 0` — big indexes take a while, and atomicity, not a
  timeout, is what protects you.

**Operational rule already learned the hard way** (`km_never_manually_apply_migrations`): never
hand-apply a migration with `psql`. The runner tracks versions; a manual apply leaves it unaware, so
the next deploy re-runs a non-idempotent statement and fails.

**Backup/restore is split dev vs prod on purpose.** `db/scripts/backup.sh` talks to the compose
service with a 14-day retention; `Deploy/db-backup.sh` talks to the named prod container with 90-day
retention. The `pg_dump` flags are **deliberately identical** (`-Fc -Z 6 --no-owner
--no-privileges`) so a dev dump and a prod dump are interchangeable for `pg_restore`. Dumps are
written atomically (`.partial` → `mv`) and the retention prune runs **only after** the new dump is
durable — so a failed backup can never delete the last good one.

---

## 4. Testing — the part 3b should copy soonest

9b's `TESTS.md` names one authoritative gate, `Deploy/local-test.sh`, which reproduces CI **plus**
the suites CI omits. Two ideas matter:

**Every suite runs in a container pinned to CI's toolchain** — `node:22-slim` for JS,
`python:3.12` for Python — with `node_modules` as a per-run anonymous volume so deps install fresh
against the lockfile. The header is explicit that the host runs Python 3.14 while the project
targets 3.12.

> **This is 3b's problem exactly.** M's host Python is 3.14; 3b's containers are `python:3.12`. Today
> "87 tests pass on M" and "CI is green" are two different statements, and only one of them is about
> the thing we ship. Porting this is cheap and closes P9.

**Hard gates vs soft gates.** Hard gates (lint, type check, tests, secret scan) fail the run. Soft
gates (`npm audit`, `pip-audit`, ingest lint) are reported but non-blocking, mirroring CI's
`|| true`. And `TESTS.md` has a third section — **"Not run by this gate (run elsewhere, by
design)"** — which is the honest bit most manifests omit: it names what *isn't* covered and where it
runs instead. `--fast` exists for the inner loop and is labelled **NOT a gate**.

**CI lessons visible in the comments**, each of which reads like a scar:

- A job name is pinned verbatim because it's a **required status check** in branch protection —
  renaming it makes the required context never report, blocking every PR.
- The client test step was added because the client suite was previously **CI-invisible**: a client
  test could go red and still merge.
- Supply-chain audit (`pip-audit`) was moved **into CI** after Phase A learned it doesn't survive on
  an operator's checklist.
- The ingest job lint+audited its tooling but **never ran its tests** — two red parser tests shipped
  unnoticed as a result.
- Every exclusion carries a written justification and a tracking ID, rather than a bare `--ignore`.

**Lint as a guardrail, not just style.** 9b's ESLint config uses `no-restricted-imports` so only
`services/claude/client.ts` may import the Anthropic SDK and only `src/db/pool.ts` may construct a
pg `Pool` — architecture enforced by the linter. 3b's equivalent would be: only the provider layer
may import `yfinance`/the FMP client, and only one module may place an order.

---

## 5. Security documentation

Six `SECURITY.md` files, split by surface: `Deploy/` for the hosting surface, then `server/`,
`client/`, `db/`, `db/migrations/`, `services/kiwi/`, and `server/src/services/claude/` for app
surfaces. Every entry is literally:

```
* Vector:  <the concrete attack>
  Defense: <what stops it, and where in the code>
```

Sections in the deploy doc: authentication & authorization, secret handling, public-ingress posture,
Cloudflare Tunnel posture, nginx hardening, backups. Concrete defenses worth copying verbatim:

- `.env` is `chmod 0600`; `.env.example` ships **placeholders only**; no deploy script ever `echo`s a
  secret (a reference implementation's debug `echo` was deliberately not carried over).
- Rotation is a single edit-in-place plus a restart — there is no second copy of the secret (no CI
  variable store) that could drift.
- TLS terminates at the Cloudflare edge, so nginx sets `X-Forwarded-Proto https` and
  `X-Forwarded-Port 443` to keep `Secure` cookies and correct absolute URLs — and the app trusts
  those headers *only because* the LB is the sole ingress.
- No public DNS A record points at the host IP, so the origin can't be reached around Cloudflare.
- `server_tokens off`; a single anchored regex location for the exact set of API prefixes;
  `proxy_pass` to a bare upstream so the URI is preserved verbatim.
- `proxy_buffering off` with timeouts *longer* than the app's own upstream timeout, so SSE/streaming
  works. **3b's dashboard is SSE-heavy** (scan, debate, pipeline all stream) — this one is not
  optional.

Two traps recorded elsewhere that apply directly:

- **`.gitleaks.toml` must keep `[extend] useDefault = true`.** A custom config otherwise *replaces*
  the default ruleset and silently disables all secret scanning.
- **The nginx API route allow-list must list every top-level API prefix.** A new prefix that isn't
  in the regex gets shadowed by the SPA catch-all and returns `200 text/html` instead of the API's
  JSON — a failure that looks like an app bug.

---

## 6. Documentation conventions

Per feature: `BUILD_<feature>.md` → `REVIEW_<area>.md` → `FIX_REPORT_<feature>.md` →
`REVIEW_FIXES_<feature>.md`. That's the `/fixpass` paper trail, one file per stage, kept in `docs/`.

Decisions get **ADRs** (`db/docs/ADR-001-database-choices.md` … `ADR-013`) with a numbering policy in
`db/docs/README.md`. 3b's Phase 2 will make several decisions worth recording this way: the provider
interface, the schema shape, the migration-runner choice, and the caching/call-budget strategy.

---

## 7. What to port, in order

| When | What | Closes |
|---|---|---|
| Phase 1 | `SECURITY.md` per surface (`deploy/`, `backend/`); `.gitleaks.toml` with `useDefault = true` | P8 |
| Phase 2 | The `db/` harness shape: hand-rolled runner, `NNN_<name>.{up,down}.sql`, checksums, runner-owned transactions, destructive gate, testcontainers suite, dev/prod backup split | P6 |
| Phase 2 | Internal no-egress network for Postgres; loopback-only binding; name-pinned external volumes with a single idempotent creator | P6, P8 |
| Phase 2 | Lint-enforced architecture boundaries (only the provider layer imports a data client; only one module places an order) | P5 |
| Phase 6 | `local-test.sh` equivalent — suites in `python:3.12` containers; hard vs soft gates; an honest "not covered here" section in `TESTS.md` | P9 |
| Phase 6 | `cloudflared-setup.sh` (named tunnel, idempotent); ingress posture; SSE-safe proxy settings | P7 |
| Phase 6 | Cold-boot vs steady-state split; a `bg-health.sh` equivalent; a documented break-glass rebuild | P7 |

**Deliberately not ported:** blue/green two-color deploys (overkill at this user count — revisit as owners are added),
the Azure pipeline, and the multi-user governance surface.
