# Review: database infrastructure and security posture

**Reviewer:** independent (did not write this code) · **Date:** 2026-07-28 · **Tree:** `f0e49ee`

**Scope reviewed:** `docker-compose.db.yml` · `db/Dockerfile` · `bin/db_up.sh` · `bin/db_psql.sh` ·
`bin/db_migrate.sh` · `bin/lib_ports.sh` · `db/.env.example`.
Out of scope (other reviewers own them): `db/migrate.py`, `db/migrations/*.sql`.

Every claim below was verified against the live stack on M. Where a finding says "proved", the
command and its output are reproduced in the detailed section. `rh-db` was left running and healthy
with migrations 001–003 applied; `db/.env` was restored byte-identical at mode 600; no `km-*` object
was touched.

---

## Summary verdict

**REQUEST CHANGES** — 5 BLOCKER, 11 SHOULD-FIX, 9 NIT, 10 PRAISE.

The architecture here is good and several details are better than the surrounding ecosystem. The
problem is not the design; it is that **five of the security properties this code asserts in its own
comments do not hold when tested**, and the project has already been burned once by exactly that
pattern (`SECURITY_FINDINGS_2026-07-27.md` — the "verified against the source before being written
down" discipline is the right instinct; it was not applied to these seven files).

Every blocker is a small fix. None requires redesign. The reason they are blockers rather than
should-fixes is that each one is a comment that would stop a future reader from looking — which is
strictly worse than no comment at all.

---

## Bar checklist

| Bar rule | Verdict | Note |
|---|---|---|
| §0 Robust by default (fail closed, fail loud) | **PASS** | Empty / partial `db/.env` → exit 1 naming the variable; stopped container → clear refusal from both wrappers; `db_up.sh` polls real health and detects `exited` instead of sleeping. Verified. |
| §0 Clean tree (no hardcoded secrets/paths) | **PASS** | `db/.env` gitignored (`.gitignore:43`), gitleaks in CI, only placeholders in `.env.example`. |
| §0 Comments explain *why*, and are true | **FAIL** | 5 comments assert properties that do not hold. B1–B5. |
| §3.5 No unvalidated interpolation into an interpreter | **FAIL** | `lib_ports.sh:85` interpolates `PORT_MIN`/`PORT_MAX` into `python3 -c`. N1. |
| §3.6 No secrets in git / files / images | **PARTIAL** | Gitignored and gitleaks-scanned — good. But the secret hits `argv` (B1), the file is 0664 before it is 0600 (B2), and it sits in the `db/` build context with no `.dockerignore` (S7). |
| §3.6 Rotation on schedule / on exposure | **FAIL** | The documented rotation procedure does not rotate. B4. |
| §3.11 / §6.10 Pin exact versions, lockfiles, SCA | **PARTIAL** | Both base images digest-pinned (verified upstream); `psycopg==3.2.3` exact-pinned but 13 releases stale and not hash-pinned; transitive deps float. S6. |
| §4.8 DB-side `statement_timeout` + `idle_in_transaction_session_timeout` | **FAIL** | Both set to `0` (unlimited) globally; the comment describes the inverse. S3. |
| §4.9 App connects as a least-privilege role, never superuser | **FAIL** | `rh` is `rolsuper=t` and is the only login role. `COPY … FROM PROGRAM` proved available. S9. |
| §4.10 Automated, tested backups for stateful volumes | **FAIL** | No backup path for `rh_db_data` at all. S10. |
| §6.1 Digest-pinned base, numeric non-root `USER`, exec-form entrypoint | **PASS** | All three present in `db/Dockerfile`. |
| §6.1 Multi-stage build | **PASS (intent)** | Single-stage, but `python:3.12-slim` + a `[binary]` wheel means no toolchain to strip — verified `gcc/cc/make/ld` all absent from the final image. The rule's purpose is met. |
| §6.1 `.dockerignore` present | **FAIL** | No `db/.dockerignore`; the root one does not apply to a `db/` context. S7. |
| §6.2 Image scanning + SBOM + signing in CI | **FAIL** | No Trivy/Grype/Syft/cosign anywhere in `.github/workflows/`. S5. |
| §6.3 `no-new-privileges`, `cap_drop: [ALL]`, no privileged, no docker socket | **PASS** | Verified live: `SecurityOpt=[no-new-privileges:true]`, `CapDrop=[ALL]`, `Privileged=false`, `CapBnd=0xcb`, PID 1 uid 70 with `CapEff=0`, `NoNewPrivs=1`. |
| §6.3 `read_only: true` rootfs | **FAIL** | `ReadonlyRootfs=false`. S8. |
| §6.4 Secrets via gitignored `.env`, never committed | **PASS** | |
| §6.4 Healthcheck on every service | **PARTIAL** | Present and correctly IPv4-explicit, but validates nothing beyond "the postmaster answers". S4. |
| §6.4 `deploy.resources.limits` on every service | **PASS** | **Honoured** — verified `Memory=2147483648`, `NanoCpus=2000000000` on the live container. Compose v5.3.1 applies these in non-swarm mode. Not a fiction. |
| §6.4 Bind loopback-only | **N/A** | No host port by design (ADR-001). |
| §6.6 All config via env, fail fast on missing | **PASS** | `${VAR:?message}` on all three credentials in compose, plus `: "${VAR:?}"` guards in both scripts. |
| §6.7 Distinct liveness vs readiness | **FAIL** | One probe used as both; it is a liveness signal reported as readiness. S4. |
| §6.7 Structured logs, log-size caps | **PASS** | `json-file`, `max-size: 10m`, `max-file: 5` — verified applied. |
| **Standing rule: verify port free, never disturb the holder** | **FAIL** | `lib_ports.sh` returns a false "free" for 9b's own 1840–1841. B3. |
| **Namespace isolation from 9b** | **PASS** | Zero name overlap; cross-bridge reach to `rh-db` from another container blocked by Docker isolation (verified); no `down`/`-v`/`prune`/`docker rm` anywhere in these files. |

---

## Findings

### BLOCKER

- **B1** — `bin/db_up.sh:42-47`. The comment says the secret "travels in a variable, never in a
  command-line argument that would be visible in `ps`". `awk -v pw="$generated"` puts the password
  **in awk's argv**. Proved.
- **B2** — `bin/db_up.sh:47-49`. `db/.env` is created by shell redirect at umask mode (**0664** on
  this box) and `chmod 600` runs *after* awk has already written the password. `db_up.sh:8` claims
  "written 0600". Proved.
- **B3** — `bin/lib_ports.sh:36-40` and its header at `:9-12`. Two independent defects: published
  **port ranges are not parsed at all** (9b's `0.0.0.0:1840-1841->1840-1841/tcp` yields nothing), and
  `docker ps` without `-a` **cannot see stopped containers**, which the header states is the entire
  reason check #3 exists. Both proved. This produces a false "free" verdict on 9b's ports — the exact
  failure the standing rule exists to prevent.
- **B4** — `db/.env.example:11-12`. "Rotate by editing db/.env and recreating the container" does not
  rotate anything. `POSTGRES_PASSWORD` is consumed only by `initdb`; against an existing volume the
  old password stays valid and the new one fails — and the healthcheck reports healthy either way, so
  `db_up.sh` prints `✓ Postgres healthy` over a broken credential.
- **B5** — `docker-compose.db.yml:10,14`, echoed from `ADR-001` "Consequences → Good". The header
  claims a loopback host port that line 57 deliberately does not create, references a `.env.db` and a
  `bin/pick_db_port.sh` that **do not exist**, and inherits the ADR's claim that nothing on the box
  outside `rh-internal` can reach the database. **The host can**: `172.22.0.2:5432` accepted a TCP
  connection and a Postgres startup packet from an ordinary host process. Proved.

### SHOULD-FIX

- **S1** — `bin/db_migrate.sh:54`. `DATABASE_URL` is string-concatenated with no percent-encoding. A
  hand-edited password containing `@` **redirects the connection to a different host**; `/` scrambles
  host/port/dbname; `%` hard-errors. Proved against psycopg's own parser.
- **S2** — `bin/db_up.sh:60`, `bin/db_migrate.sh:44`. `source db/.env` executes the file as shell.
  Proved: `POSTGRES_PASSWORD=a$(id -u)b` → password became `a1000b`; `POSTGRES_PASSWORD=pa ss` →
  executed `ss` and dumped the socket table. Directly on the "edit db/.env" path B4 documents.
- **S3** — `docker-compose.db.yml:50-52`. `statement_timeout=0` and
  `idle_in_transaction_session_timeout=0` are set **server-wide**, i.e. unlimited for every session
  including the future app. The comment says "Loader sessions raise these locally (`SET LOCAL`), so
  the server default stays sane for the app" — that describes the opposite of what the code does.
  Violates §4.8 [P1]; an idle-in-transaction session now holds locks and blocks vacuum indefinitely.
- **S4** — `docker-compose.db.yml:78`. `pg_isready` does not connect or authenticate. Proved:
  `pg_isready -d THIS_DB_DOES_NOT_EXIST` and `-U nosuchuser -d nosuchdb` both exit 0. The
  `-U`/`-d` arguments are decorative. `db_up.sh:76` reports success on this signal.
- **S5** — No image scanning, SBOM, or signing in CI. `.github/workflows/ci.yml` runs ruff + pytest +
  a frontend build; `gitleaks.yml` covers secrets only. §6.2 is [P0]. Note this is *narrower* than
  the still-open F8 in `SECURITY_FINDINGS_2026-07-27.md` (which covers app deps) — the new images add
  a second unscanned surface.
- **S6** — `db/Dockerfile:19`. `psycopg[binary]==3.2.3`; current is **3.3.4** (13 releases). Not
  hash-pinned (`--require-hashes`), and `psycopg-binary` / `typing_extensions` float. §3.11 [P0].
- **S7** — `bin/db_migrate.sh:39` builds with context `db/`, which contains **`db/.env` with the live
  password**, and there is no `db/.dockerignore` (the root `.dockerignore` is context-relative and is
  not read for a `db/` context). Nothing `COPY`s it today, so it does not reach a layer — but the
  secret is uploaded into the daemon's build context, and one added `COPY` bakes it in permanently.
- **S8** — `docker-compose.db.yml` omits `read_only: true` (§6.3 [P1]), `pids_limit`, and
  `memswap_limit`. Verified `MemorySwap=4294967296` — the "2g" cap actually permits 2 GiB RAM **plus
  2 GiB of host swap**, on a box concurrently running a 9b GPU batch.
- **S9** — `POSTGRES_USER=rh` is `rolsuper=t` and the only login role (`db/.env.example:8`). §4.9 is
  [P0]. `COPY … FROM PROGRAM 'id'` executed successfully as uid 70 inside the container. ADR-001 is
  honest that network isolation is the only control here; the fix is to provision `rh_app` in
  migration 001 and use it for runtime, keeping `rh` for DDL.
- **S10** — No backup for `rh_db_data`. §4.10 is [P0], and ADR-001 calls this data "the record the
  whole learning loop is built on". 9b runs a dedicated `km-backup` container; 3b has nothing.
  Separately, the volume carries `com.docker.compose.project=rh-db`, so
  `docker compose -p rh-db -f docker-compose.db.yml down -v` would delete it. No script does this,
  but nothing marks the volume `external: true` either.
- **S11** — `bin/db_migrate.sh:35-40`. "Build only when the image is absent **or its inputs changed**"
  — it only tests absence. Bumping the `psycopg` pin in `db/Dockerfile` is silently ignored until
  someone manually removes `rh-migrate:local`.

### NIT

- **N1** — `bin/lib_ports.sh:85` interpolates `${PORT_MIN}`/`${PORT_MAX}` unvalidated into
  `python3 -c "..."`. Same-privilege, so not an escalation, but it is exactly the shape §3.5 bans;
  pass them as `sys.argv` instead. `PORT_MAX < PORT_MIN` also produces a raw Python traceback rather
  than a message.
- **N2** — `bin/lib_ports.sh:78` documents `pick_free_port [exclude...]` (varargs) but line 81 reads
  only `$1`. Doc/code mismatch.
- **N3** — `bin/lib_ports.sh:64-75` binds `AF_INET` only. An IPv6-only listener (`IPV6_V6ONLY`) reads
  FREE from the bind test; UDP is invisible entirely. Both proved. Check #2 (`ss`) covers the IPv6
  case, so the *combined* verdict is still correct — worth a comment saying so, since the header
  currently claims check #1 is "authoritative".
- **N4** — `bin/db_psql.sh:33`. `docker exec` inherits the image's empty `Config.User`, so the psql
  session runs as **root inside the container** (`uid=0`). Not an escape — `CapEff=0` on PID 1,
  `no-new-privileges` set — but `--user postgres` costs nothing. Relatedly, the `PGPASSWORD` at
  line 34 is decorative: `pg_hba.conf` is `trust` for `127.0.0.1/32` inside the container (the image
  default; `scram-sha-256` correctly applies to every other host, verified).
- **N5** — `bin/db_psql.sh:16` hardcodes `CONTAINER_NAME`; `${RH_DB_CONTAINER:-rh-db}` would let the
  wrapper be pointed at a restore/staging instance.
- **N6** — No `stop_grace_period`. `STOPSIGNAL=SIGINT` (fast shutdown) is inherited correctly, but
  with `shared_buffers=512MB` the shutdown checkpoint can exceed the 10 s default and earn a SIGKILL
  plus crash recovery. `stop_grace_period: 60s` is the usual guard.
- **N7** — `bin/db_migrate.sh` builds the image (line 37-40) *before* validating credentials (line
  47-49). A missing `POSTGRES_USER` costs a full image build first.
- **N8** — `bin/lib_ports.sh:1` has a shebang but the file is mode 0664 and lines 26-29 guard against
  execution. The shebang is vestigial (harmless — it does buy editor/shellcheck detection).
- **N9** — `db/__pycache__/` is present in the working tree and therefore in the `db/` build context.
  The root `.dockerignore` excludes it, but that file does not apply here (see S7).

### PRAISE

Name these explicitly so a later refactor does not quietly undo them.

- **P1 — `bin/db_psql.sh:33-35` argument passing is exactly right.** `sh -c '…"$@"' -- "$@"` places
  `--` as `$0` and the user's arguments as `$1..$n`, and because they travel as an argv array rather
  than through a second round of expansion, metacharacters stay literal. Verified with `$(id)`,
  backticks, `;rm -rf /`, multi-statement SQL, zero-arg interactive, and `-f -` on a pipe. This looks
  like something a future reader would "simplify" into a broken `sh -c "psql $*"`. It should not be.
- **P2 — the bind probe deliberately omits `SO_REUSEADDR`, and that is load-bearing.** Verified: a
  holder on `127.0.0.1:P` **that itself sets `SO_REUSEADDR`** (which nearly every real server does)
  is still correctly detected as BUSY by the `0.0.0.0` probe, precisely because the probe does not
  set the option. Setting it "for convenience" would silently invert the answer.
- **P3 — the `ss` parser is genuinely robust.** Correct on `*:8080`, `[::]:9090`, `127.0.0.1:7070`,
  and `[fe80::1%eth0]:6060`. Tested.
- **P4 — both image digests are real and current.** `docker buildx imagetools inspect` against Docker
  Hub returns exactly the two pinned digests, and both are **multi-arch index** digests, so the pin
  stays portable. The "how to bump" instructions in both files are correct.
- **P5 — `deploy.resources.limits` is not a fiction here.** Compose v5.3.1 applies it in non-swarm
  mode: `Memory=2147483648`, `NanoCpus=2000000000` on the live container. Worth recording, because
  the "compose ignores `deploy:`" folklore is from Compose v1 and would otherwise get this deleted.
- **P6 — the egress block is real, including the DNS channel.** TCP to `1.1.1.1` returns
  `Network unreachable`; and external DNS through Docker's embedded resolver **SERVFAILs** rather
  than forwarding. DNS-based exfiltration is the vector that usually survives `internal: true`, and
  it is closed. This is the strongest thing in the change.
- **P7 — fail-closed behaviour is consistent and loud.** Empty `db/.env` → exit 1 naming the missing
  variable; partial `db/.env` → same; stopped container → both wrappers refuse with the remedy in the
  message; `db_up.sh` polls the real healthcheck and short-circuits on `exited`/`dead` instead of
  burning the full 120 s. All four tested. This is the §0 [P1] rule actually implemented, not
  asserted.
- **P8 — the `cap_add` list is minimal and correct, not over-granted.** `CapBnd=0x00000000000000cb`
  is exactly `{CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID}` — nothing extra slipped in — and the
  postmaster itself ends up as uid 70 with `CapEff=0000000000000000` and `NoNewPrivs=1`. All five are
  genuinely required: the official entrypoint runs as root to chown `PGDATA` and `su-exec`s down on
  every start, not only on first init.
- **P9 — namespace hygiene is clean and verified.** `rh-db` / `rh_db_data` / `rh-internal` / project
  `rh-db` have zero overlap with any `km-*` object; `rh-db` cannot reach `km-db` (proved), and the 3b
  backend container on a different bridge cannot reach `rh-db` (proved — Docker inter-bridge
  isolation). There is **no** `down`, `-v`, `prune`, `docker rm`, or `docker volume rm` anywhere in
  these seven files. No path in this change can touch 9b.
- **P10 — the healthcheck targets `127.0.0.1` rather than `localhost`, with the reason recorded** in
  the comment. That is a real bug 9b hit, fixed here before it could recur. Keep the comment.

---

## Detailed findings

### B1 — the generated DB password *is* visible in `ps` (`bin/db_up.sh:42-47`)

```
# Use awk rather than sed so the secret travels in a variable, never in a command-line argument
# that would be visible in `ps` while the process runs.
awk -v pw="${generated}" '
```

`awk -v pw=VALUE` is a command-line argument. The shell expands `"${generated}"` before `execve`, so
the plaintext password lands in `awk`'s `argv[2]`:

```
$ awk -v pw="SUPERSECRET_MARKER_abc123" '{print}' fifo &
$ ps -p $! -o args=
awk -v pw=SUPERSECRET_MARKER_abc123 {print} fifo
$ ls -l /proc/$!/cmdline
-r--r--r-- 1 jared-williams jared-williams 0 /proc/1971403/cmdline
$ grep hidepid /proc/mounts        # → no match
proc on /proc type proc (rw,nosuid,nodev,noexec,relatime)
```

`/proc/PID/cmdline` is mode 0444 and `/proc` is mounted **without `hidepid`**, so any uid on the box
— every system service account, every container sharing the host PID namespace — can read it for the
lifetime of the awk process. The window is short, but "short" is not the property the comment claims,
and B5 makes this password the primary access control from the host rather than a defence-in-depth
layer.

The comparison in the comment is also inverted: `sed -i "s/…/${pw}/"` and `awk -v pw="${pw}"` expose
the secret identically. The distinction that matters is *argv vs. stdin/env*, not *sed vs. awk*.

**Fix (either):**

```bash
# (a) environment, not argv — /proc/PID/environ is 0400 owner-only
generated="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
PW="${generated}" awk '
  /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" ENVIRON["PW"]; next }
  { print }
' db/.env.example > "${DB_ENV}"

# (b) never leave Python — no second process sees it at all
python3 - "$PROJECT_DIR/db/.env.example" "$DB_ENV" <<'PY'
import os, secrets, sys
src, dst = sys.argv[1], sys.argv[2]
pw = secrets.token_urlsafe(32)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # also fixes B2
with os.fdopen(fd, "w") as out, open(src) as inp:
    for line in inp:
        out.write(f"POSTGRES_PASSWORD={pw}\n" if line.startswith("POSTGRES_PASSWORD=") else line)
PY
```

Option (b) closes B1 and B2 in one move and is the one I would take.

---

### B2 — `db/.env` is world-readable before it is 0600 (`bin/db_up.sh:47-49`)

```bash
  ' "${PROJECT_DIR}/db/.env.example" > "${DB_ENV}"     # ← creates the file at 0666 & ~umask
  unset generated
  chmod 600 "${DB_ENV}"                                # ← too late
```

`db_up.sh:8` states the file is "written 0600". It is not — the shell creates it via `open(…, 0666)`
masked by the umask, and only then is it narrowed:

```
$ umask
0002
$ ( awk 'BEGIN{print "x"}' > modetest ); stat -c '%a %n' modetest
664 modetest
```

So there is a window in which `db/.env`, containing the live Postgres password, is **group- and
world-readable**. It is milliseconds wide in the happy path — but it is unbounded if the script is
interrupted between the redirect and the `chmod` (Ctrl-C, OOM kill, a `set -e` abort), and the file
then persists at 0664 with a valid password in it. Note that `db_up.sh:52-54` warns about a loose
mode on *subsequent* runs, which shows the risk was understood; the creating path just misses it.

**Fix:** `umask 077` immediately before the redirect (and restore after), or the `O_EXCL | 0o600`
approach in B1(b). `umask 077` also has the merit of covering any future file this script writes.

---

### B3 — `lib_ports.sh` reports 9b's ports 1840–1841 as free (`bin/lib_ports.sh:9-12, 36-40`)

The header states check #3's purpose:

```
#   3. docker ports  — catches a port published by a running container. A container that is
#                      *stopped* still owns its port mapping in compose terms, and `ss` won't
#                      show it; taking that port would break the stack on its next start.
```

and the implementation is:

```bash
docker_published_ports() {
  command -v docker >/dev/null 2>&1 || return 0
  docker ps --format '{{.Ports}}' 2>/dev/null \
    | grep -oE ':[0-9]+->' | tr -dc '0-9\n' || true
}
```

**Defect 1 — port ranges are not parsed.** `docker ps` renders a published range as
`0.0.0.0:1840-1841->1840-1841/tcp`. The regex `:[0-9]+->` requires the digits to be immediately
followed by `->`; in `:1840-1841->` they are followed by `-`, so **there is no match at all**. Run
against the live box:

```
$ docker ps --format '{{.Ports}}'
...
80/tcp, 0.0.0.0:1840-1841->1840-1841/tcp, [::]:1840-1841->1840-1841/tcp   ← km-lb
127.0.0.1:1842->4000/tcp                                                  ← km-server-blue
...
$ docker_published_ports
20363
26595
1842
1843

port 1840 : docker=MISSED  ss=SEEN
port 1841 : docker=MISSED  ss=SEEN
port 1842 : docker=SEEN    ss=SEEN
```

The two ports the header names by number as the reason this library exists — 9b's `km-lb` on
1840–1841 — are precisely the two it cannot see.

**Defect 2 — the stated purpose is unreachable by construction.** `docker ps` without `-a` lists only
running containers, so it can never see a stopped one. And `docker ps -a` does not help either:
Docker renders an empty `.Ports` for a stopped container. Proved with a throwaway:

```
$ docker run -d --name rhrev-porttest -p 127.0.0.1:30222:80 …
-- while RUNNING --   docker sees 30222: YES   port_is_free -> BUSY (good)
$ docker stop rhrev-porttest
-- after STOP --      docker ps -a ports: []
                      docker sees 30222: NO    port_is_free -> FREE   ← claim does not hold
```

**Why this is a blocker and not a nit.** Compose the two defects with the standing rule. 9b runs
blue/green; during a flip `km-lb` is stopped. In that window ports 1840–1841 pass check #1 (bind
succeeds — nothing is listening), check #2 (`ss` shows nothing), and check #3 (range not parsed *and*
stopped container invisible). `pick_free_port` would hand 1840 to a new stack, whose container then
holds the port when `km-lb` tries to come back. That is a 9b outage caused by a library whose entire
reason for existing is to prevent it.

**Fix:** read the authoritative source — the container config, which survives a stop — and expand
ranges:

```bash
docker_published_ports() {
  command -v docker >/dev/null 2>&1 || return 0
  # HostConfig.PortBindings persists across stop; docker ps --format '{{.Ports}}' does not.
  docker ps -aq 2>/dev/null | xargs -r docker inspect \
      --format '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}}
{{end}}{{end}}' 2>/dev/null \
    | grep -oE '^[0-9]+(-[0-9]+)?$' \
    | awk -F- 'NF==1 {print $1} NF==2 {for (i=$1; i<=$2; i++) print i}' \
    | sort -un || true
}
```

`HostPort` in `PortBindings` is itself range-capable (`"1840-1841"`), hence the `awk` expansion.
Please add a regression check for both the range form and the stopped-container form — this is the
kind of parsing that rots silently.

---

### B4 — the documented password rotation does not rotate (`db/.env.example:11-12`)

```
# Generated; never a real value in this example file. Rotate by editing db/.env and recreating the
# container (the volume, and therefore the data, is preserved).
POSTGRES_PASSWORD=replace-me
```

`POSTGRES_PASSWORD` is read by the official image's entrypoint **only during `initdb`**, i.e. only
when `PGDATA` is empty. Recreating the container against the existing `rh_db_data` volume skips
`initdb` entirely, so the role's password in `pg_authid` is unchanged. The result of following this
instruction is:

1. the operator believes the credential was rotated — it was not, and the old password remains valid
   (§3.6 [P1] "rotate immediately on suspected exposure" is therefore unachievable by the documented
   route, which matters given B1/B2);
2. Compose *does* recreate the container, because `POSTGRES_PASSWORD` is part of the service
   config-hash — so this is a real restart, not a no-op;
3. `db_up.sh` then reports `✓ Postgres healthy`, because `pg_isready` never authenticates (S4,
   proved below);
4. the first thing that actually authenticates — `bin/db_migrate.sh` — fails with
   `password authentication failed`, at which point the state is confusing rather than obvious.

**Fix:** correct the comment and give the real procedure, ideally as a `bin/db_rotate.sh`:

```
# Rotation changes TWO things and both are required:
#   1. bin/db_psql.sh -c "ALTER ROLE rh WITH PASSWORD 'new-secret';"   ← the database
#   2. the POSTGRES_PASSWORD line in db/.env                          ← what clients send
# POSTGRES_PASSWORD is consumed only by initdb; editing it alone changes nothing in the database.
# The container does not need recreating — only clients that cached the old value do.
```

Order matters: do the `ALTER ROLE` first, then update `db/.env`, so no window exists where clients
hold a password the server has already rejected. Applying this fix also removes the temptation to
hand-edit the password (S1/S2).

---

### B5 — the reachability story in the compose header is stale and overstated

`docker-compose.db.yml:10, 13-19`:

```
# Brought up by bin/db_up.sh, which supplies DB_PORT (from .env.db, verified free at creation) and
…
# Security posture, per SENIOR_ENGINEER_BAR §6.3/§6.4 and 9b's Deploy/SECURITY.md:
#   * host port bound to 127.0.0.1 ONLY — nothing off-box may reach the database;
```

Three problems, in ascending order of importance.

**(a) It contradicts line 57 of the same file** — `# NO `ports:` stanza, deliberately`. There is no
host port at all, so there is nothing "bound to 127.0.0.1". A reader who stops at the header comes
away with the pre-ADR design.

**(b) `DB_PORT`, `.env.db` and `bin/pick_db_port.sh` do not exist.**

```
$ ls bin/pick_db_port.sh .env.db
ls: cannot access 'bin/pick_db_port.sh': No such file or directory
ls: cannot access '.env.db': No such file or directory
$ grep -c DB_PORT docker-compose.db.yml
0
```

`db/.env.example:5-6` carries the same dangling reference. These are leftovers from the design
ADR-001 rejected and should be deleted.

**(c) The substantive claim is wrong.** ADR-001 asserts, and the header's spirit repeats, that the
posture is *stronger* than a loopback bind because "neither can anything on the box that is not on
`rh-internal`" reach the DB. Docker's `internal: true` removes the network's default route and NAT —
it does **not** remove the host's own IP on the bridge. The host routes straight to the container:

```
$ docker inspect rh-db --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
172.22.0.2
$ python3 -c "import socket; socket.create_connection(('172.22.0.2',5432),timeout=5); print('ok')"
TCP CONNECT SUCCEEDED from host to 172.22.0.2:5432
$ # …and it speaks Postgres:
server replied to startup, first byte: b'R'   (R = AuthenticationRequest)
```

Docker's inter-bridge isolation *does* hold — a container on another network cannot get there
(verified from `3brobinhoodagentic-backend-1`: timeout), and `rh-db` cannot reach `km-db` (verified:
`nc rc=1`). So off-box and cross-stack are genuinely closed. **On-box is not.** Any local user can
open a socket; `pg_hba` then requires `scram-sha-256` for non-loopback sources (verified — a rogue
container on `rh-internal` was refused with both an empty and a wrong password), so the password in
`db/.env` is the control.

That is exactly the same security model as a `127.0.0.1:PORT` bind protected by a 0600 credential
file — not a stronger one. Which is fine, and the egress block (P6) remains a genuine and valuable
win on its own terms. But it means B1 and B2 are not theoretical hygiene: **the password is the
boundary**, so leaking it to `argv` or leaving it 0664 removes the only on-box control.

It also means ADR-001's "Bad" consequence — *"A Python process in the project venv cannot open a
socket to the database"* — is factually wrong. The real reason not to rely on `172.22.0.2` is that
the address is unstable across container recreation, which is a good reason and a different one.

**Fix:** rewrite the header block to match the file (no host port; on-box reachable via the bridge IP
but credential-gated; off-box and cross-network closed), delete the `DB_PORT`/`.env.db` references
here and in `db/.env.example:5-6`, and correct the two claims in ADR-001. If on-box isolation is
actually wanted, that is a host firewall rule on `172.22.0.0/16` — a separate, deliberate decision.

---

### S1 — `DATABASE_URL` is unencoded string concatenation (`bin/db_migrate.sh:54`)

```bash
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DB_HOST}:5432/${POSTGRES_DB}"
```

Passing it via `--env DATABASE_URL` (name only, line 56) is correct and keeps it out of `argv` —
that part is right. The construction is not. Tested against psycopg's own parser, inside the runner
image:

| password | resulting `host` | resulting `dbname` | resulting `password` |
|---|---|---|---|
| `aB3-_xY9` (what `token_urlsafe` emits) | `rh-db` | `robinhood_agentic` | `aB3-_xY9` ✅ |
| `pa@ss` | **`ss@rh-db`** | `robinhood_agentic` | **`pa`** (truncated) |
| `pa/ss` | **`rh`** | **`ss@rh-db:5432/robinhood_agentic`** | **`None`** |
| `pa%ss` | — | — | `ProgrammingError: invalid percent-encoded token` |
| `pa#ss`, `pa:ss`, `pa?ss`, `pa ss` | `rh-db` | `robinhood_agentic` | correct |

The `@` row is the one that matters: the password is silently truncated *and the connection target
is taken from the password*. Made concrete —

```
password = '@km-db:5432/korean_master'
→ {'user': 'rh', 'host': 'km-db', 'port': '5432',
   'dbname': 'korean_master@rh-db:5432/robinhood_agentic'}
```

`rh-internal` has no route to `km-db` so this particular attempt fails, but the mechanism is real:
a password character can redirect where the migration runner connects and what partial credential it
sends there. Today's generated passwords are safe — `secrets.token_urlsafe` emits only
`[A-Za-z0-9_-]` — so this is latent, reachable via B4's "edit db/.env" instruction.

**Fix — do not build a URL at all.** libpq reads these directly, with no parsing layer to get wrong:

```bash
docker_flags=(--rm --network "${NETWORK}"
  --env PGHOST="${DB_HOST}" --env PGPORT=5432
  --env PGUSER --env PGPASSWORD --env PGDATABASE
  --volume "${PROJECT_DIR}:/repo:ro")
```

with `PGUSER=$POSTGRES_USER PGPASSWORD=$POSTGRES_PASSWORD PGDATABASE=$POSTGRES_DB` exported. That
requires `db/migrate.py` to accept an empty DSN (`psycopg.connect("")` picks up `PG*`), which is a
one-line change in a file another reviewer owns — **flagging it as cross-cutting**. If that
coordination is unwanted, the minimum here is
`python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.stdin.read().rstrip("\n"), safe=""))'`
on the user and password. Note the [P0] §0 rule: percent-encoding is the correct path, not the easy
one.

**Also verified for this file:** the `:ro` mount is safe. `db/migrate.py` opens nothing for writing
(only `os.environ.get`, `psycopg.connect`, and reads under `migrations/`), and as the `__main__`
script it generates no `__pycache__`. Confirmed by running `status` and `up` against the live DB with
the read-only mount in place.

---

### S2 — `source db/.env` executes it as shell (`bin/db_up.sh:60`, `bin/db_migrate.sh:44`)

```bash
set -a
source "${DB_ENV}"
set +a
```

`.env` files are not shell, but `source` treats them as such. Tested:

```
case: POSTGRES_PASSWORD=a$(id -u)b   →  parsed POSTGRES_PASSWORD=[a1000b]     ← substitution ran
case: POSTGRES_PASSWORD=pa ss        →  executed `ss`, dumped the socket table, then exit 1
case: POSTGRES_PASSWORD=pa#ss        →  parsed [pa#ss]                        (ok, by luck)
case: (empty file)                   →  exit 1                                (correct)
```

The file is 0600 and self-generated, so this is not a privilege boundary today — but B4 documents
hand-editing this file as the rotation procedure, and a password with a space, a `$`, or a backtick
then either executes something or silently mis-parses. The `pa ss` case is the nastiest: it ran a
command *and* produced a partially-set variable.

**Fix:** parse instead of execute, or (better, and it composes with S1's `PG*` approach) have
`db_up.sh` write the credentials in a form both scripts read with a strict parser:

```bash
read_env_value() {   # read_env_value <file> <key>
  local v; v="$(grep -m1 -E "^${2}=" "$1")" || return 1
  printf '%s' "${v#*=}"
}
POSTGRES_PASSWORD="$(read_env_value "${DB_ENV}" POSTGRES_PASSWORD)" || die "…"
```

Keep the existing `: "${VAR:?…}"` guards — they work well and are the reason the empty-file case
already fails cleanly.

---

### S3 — the timeout settings are inverted relative to their comment (`docker-compose.db.yml:46-52`)

```yaml
      # The bulk minute-bar load is one long transaction; the default 30s/60s app-facing timeouts
      # would kill it. Loader sessions raise these locally (SET LOCAL), so the server default stays
      # sane for the app.
      - -c
      - statement_timeout=0
      - -c
      - idle_in_transaction_session_timeout=0
```

The comment describes a design where the server default is sane and the loader relaxes it locally.
The code does the reverse: it sets the **server-wide default to unlimited**, so every session —
including the future app, `db_psql.sh`, and anything that ever attaches to `rh-internal` — inherits
no statement timeout and no idle-in-transaction timeout. (There are no "default 30s/60s app-facing
timeouts" in Postgres either; both settings default to `0` upstream, so these two lines are also
no-ops that read as though they do something.)

Consequences: a runaway query on a 300M-row table runs forever against a 2 GiB memory cap; and a
session that opens a transaction and dies holds its locks and pins the xmin horizon **indefinitely**,
which stops vacuum from reclaiming anywhere in the cluster. On a box that also runs 9b, that is the
kind of thing that shows up as unexplained disk growth days later. §4.8 [P1] asks for these to be
*set*, not disabled.

**Fix — implement what the comment says:**

```yaml
      - -c
      - statement_timeout=60s
      - -c
      - idle_in_transaction_session_timeout=300s
```

and in the loader's own session, `SET LOCAL statement_timeout = 0;` at the top of the bulk
transaction. If a migration needs it too, `db/migrate.py` can issue the same `SET LOCAL` on its DDL
connection — which is also where §4.5 [P0] wants a `lock_timeout` set, so the two changes belong
together.

---

### S4 — the healthcheck proves only that the postmaster answers (`docker-compose.db.yml:74-82`)

```yaml
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U ${POSTGRES_USER} -d ${POSTGRES_DB} -q"]
```

`pg_isready` sends a startup packet and classifies the response; it never authenticates and never
opens the database. The `-U`/`-d` arguments influence only the connection string it builds:

```
$ docker exec rh-db sh -c '
    pg_isready -h 127.0.0.1 -U rh        -d robinhood_agentic        -q; echo "real          -> $?"
    pg_isready -h 127.0.0.1 -U rh        -d THIS_DB_DOES_NOT_EXIST   -q; echo "bogus db      -> $?"
    pg_isready -h 127.0.0.1 -U nosuchuser -d nosuchdb                -q; echo "bogus user+db -> $?"'
real          -> 0
bogus db      -> 0
bogus user+db -> 0
```

So the container reports **healthy** in states where nothing useful can be done with it: the volume
was initialised with a different `POSTGRES_DB`, the role does not exist, or (per B4) the password no
longer matches. `db_up.sh:76` prints `✓ Postgres healthy. Connect with: bin/db_psql.sh` on the
strength of this, and the first real error surfaces later, somewhere else.

Two things it *does* get right and which must be preserved: probing `127.0.0.1` rather than
`localhost` (P10), and the fact that during `initdb` the temporary server listens only on the unix
socket, so this probe correctly reports unhealthy while initialisation is in progress — that is what
makes `start_period: 30s` sufficient rather than a race.

**Fix — make it a readiness probe (§6.7 [P0] separates the two):**

```yaml
      test: ["CMD-SHELL", "psql -q -U \"$$POSTGRES_USER\" -d \"$$POSTGRES_DB\" -c 'select 1' > /dev/null || exit 1"]
```

Over the container's unix socket this is `trust`-authenticated, so it needs no password, and it fails
if the database or role is absent. Keep `start_period: 30s`; the socket-only initdb server means an
early attempt fails closed, which is the behaviour you want.

---

### S7 — `db/.env` is inside the migration image's build context (`bin/db_migrate.sh:39`)

```bash
  docker build -q -f "${PROJECT_DIR}/db/Dockerfile" -t "${IMAGE}" "${PROJECT_DIR}/db"
```

The context is `db/`, whose contents are:

```
-rw-------  db/.env          ← the live Postgres password
-rw-rw-r--  db/.env.example
-rw-rw-r--  db/Dockerfile
drwxrwxr-x  db/__pycache__
-rw-rw-r--  db/migrate.py
drwxrwxr-x  db/migrations
$ ls db/.dockerignore
ls: cannot access 'db/.dockerignore': No such file or directory
```

`.dockerignore` is resolved **relative to the build context**, so the project-root `.dockerignore`
(which does exclude `.env`) is not consulted at all for a `db/` context. `db/Dockerfile` contains no
`COPY`, so the secret does not reach an image layer *today* — but it is transferred to and cached by
the daemon on every build, and the safety margin is one future `COPY . .` wide. §6.1 [P1] wants a
`.dockerignore` shipped regardless.

**Fix:** add `db/.dockerignore`:

```
.env
.env.*
!.env.example
__pycache__/
*.pyc
```

Cheap, and it makes the "no secret in this image" property structural rather than incidental.

---

## Coordination observations

**Impact on 9b Korean Master — none, with one caveat.** All `km-*` containers were running and
healthy before and after this review, `km-worker`'s GPU batch was never touched, and no `km-*`
container, volume, or network was stopped, removed, or attached to. Verified that `rh-db` cannot
reach `km-db` (`nc rc=1`, egress-blocked) and that a container on another bridge cannot reach
`rh-db`. The namespace separation (`rh-db` / `rh_db_data` / `rh-internal`, compose project `rh-db`)
is clean.

**The caveat is B3**, which is a live hazard to 9b rather than an abstract one: `lib_ports.sh` cannot
see `km-lb`'s published range `1840-1841`, and cannot see any stopped container's reservation. It is
not used by the DB stack (no host port), so nothing has fired yet — but `bin/pick_ports.sh` sources
it, and that is what allocates ports for the dashboard. I would fix B3 before the next `bin/up.sh`
run, independently of whether the rest of this review is actioned.

**Actions I took on the live box, for the record:**

- Created and removed one throwaway container `rhrev-porttest` (published `127.0.0.1:30222`), used to
  prove the stopped-container case in B3. Confirmed removed.
- Stopped and restarted `rh-db` once, deliberately, to test the idempotency and failure paths in
  criterion 9. It is running and healthy, migrations 001–003 `applied / ok`.
- Temporarily replaced `db/.env` twice (empty, then partial) to test the fail-closed paths. Both
  times the script exited 1 before reaching compose, so the container was never reconfigured.
  `db/.env` was restored and verified byte-identical to the pre-review backup (`cmp` clean) at mode
  600.
- Ran `bin/db_up.sh` ×4, `bin/db_migrate.sh status` ×5, `bin/db_psql.sh` ×5. All idempotent.
- Ran a `COPY … FROM PROGRAM 'id'` inside `rh-db` (S9 evidence) against a `TEMP` table only; nothing
  persistent was written.
- `shellcheck -x` over all four scripts: **clean, rc=0**. Worth adding to CI — it would not have
  caught any blocker here, but it is free.

**Cross-reference with `SECURITY_FINDINGS_2026-07-27.md`.** Nothing in this change makes an open
finding worse or contradicts a stated fix. Two relationships worth recording:

- **F8 (deps unpinned, no scanning) is now broader, not worse.** The two new images are digest-pinned
  — genuinely better than `backend/requirements.txt` — but they add a second unscanned artifact
  class. When F8's `pip-audit` lands, the same PR should add Trivy over `rh-migrate:local` and
  `postgres:16-alpine` (S5, S6).
- **F9 (no container hardening) is partially resolved here, and this compose is now the reference.**
  `docker-compose.db.yml` does `no-new-privileges` + `cap_drop: [ALL]` + limits + log caps correctly
  and verifiably; `docker-compose.yml` and `docker-compose.prod.yml` still do none of it. When F9 is
  fixed, copy this file's approach — and copy `read_only: true` **into** it at the same time (S8), so
  the reference does not teach the omission.
