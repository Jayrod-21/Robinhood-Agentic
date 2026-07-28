# ADR-001 — The database is network-isolated and has no host port

**Status:** accepted · **Date:** 2026-07-28 · **Amended:** 2026-07-28 (fixpass — the original
overstated on-box isolation; the Consequences section below now states what was actually verified.
The decision itself is unchanged.)

## Context

The 3b database will hold position history, portfolio value series, and every agent evaluation — the
record the whole learning loop is built on. Two properties were wanted:

1. **No egress.** A container on an `internal: true` network cannot reach the internet. This is the
   structural defense against a payload using `COPY … FROM PROGRAM 'curl …'` to exfiltrate.
2. **A loopback host port**, so host-side `psql`, `pg_dump`, and a Python loader in the project venv
   can reach the database directly at `127.0.0.1:<port>`.

**These are mutually exclusive, and Docker does not tell you so.** A container attached only to an
internal network receives no port bindings; a `ports:` entry is accepted and silently ignored.
Verified here: with `ports: - "127.0.0.1:24583:5432"` declared, `docker ps` reported only
`5432/tcp` and `ss -ltn` showed nothing listening on 24583.

9b Korean Master has the same latent issue — `docker-compose.shared.yml:124-125` declares
`127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432` on `km-db` with a comment explaining that host tools use
it, and nothing is listening on 5432. Its backup and migration tooling all runs through
`docker exec` / containers on the internal network, so the dead declaration never caused a failure.
It is worth fixing there as a documentation defect.

## Decision

**Keep the network isolation. Drop the host port.**

Access paths:

- **Interactive / host use** — `bin/db_psql.sh`, a thin `docker exec rh-db psql` wrapper. Credentials
  come from the container's own environment, so no password crosses the host command line.
- **Migrations and bulk loads** — run as containers attached to `rh-internal`, the pattern 9b uses
  for `run_migrate` / `run_loader`.
- **Tests** — testcontainers spins a throwaway Postgres per run and never touches this instance.

## Consequences

**Good**

- The egress block is real and verified, including the DNS channel: TCP out returns
  `Network unreachable`, and external DNS through Docker's embedded resolver SERVFAILs rather than
  forwarding.
- Nothing off-box can reach the database, and neither can containers on other Docker bridges
  (inter-bridge isolation, verified both directions against the 3b dashboard bridge and `km-db`).
- Containerizing the migration runner and the loader also **pins their Python to 3.12**, matching the
  deploy target. M's host Python is 3.14, so this closes part of P9 (the "passes locally ≠ passes in
  CI" gap) as a side effect rather than as separate work.

**What this does NOT provide** *(correction, 2026-07-28 — the original claimed on-box isolation
stronger than a loopback bind; that claim was false and is withdrawn)*

- **On-box processes CAN reach the database.** `internal: true` removes the network's default route
  and NAT; it does not remove the host's own interface on the bridge. Verified: an ordinary host
  process opened a TCP connection to the container's bridge IP (`172.x.x.x:5432`) and completed a
  Postgres startup exchange. Those connections face `scram-sha-256` (verified: empty and wrong
  passwords refused), so **the password in `db/.env` (mode 0600) is the on-box access control**.
  The net posture equals a `127.0.0.1` bind guarded by a 0600 credential file — not stronger. If
  genuine on-box isolation is ever wanted, that is a host firewall rule on the bridge subnet — a
  separate, deliberate decision.
- Consequently, host tools *can technically* connect via the bridge IP. Don't: the IP is unstable
  across container recreation. The supported paths remain the wrapper and `rh-internal` containers.

**Bad**

- Every host tool must go through a wrapper or a container. That is real friction, and friction
  invites shortcuts — hence `bin/db_psql.sh` existing up front rather than being left as an exercise.
- A Python process in the project venv has no *stable* address for the database — only the
  per-recreation bridge IP, which nothing should hard-code. Anything needing direct access has to
  run containerized. *(Original wording claimed the venv "cannot open a socket" at all; wrong, per
  the correction above — the point that survives is instability, not impossibility.)*

**Revisit if** the friction starts producing workarounds, or if a tool genuinely cannot be
containerized. The escape hatch is to move the DB to a non-internal bridge with loopback-only
publishing, accepting egress in exchange — at which point the least-privilege `rh_app` role (no
superuser, therefore no `COPY … FROM PROGRAM`) becomes the primary control rather than
defense-in-depth. That is a deliberate trade to make explicitly, not to drift into.
