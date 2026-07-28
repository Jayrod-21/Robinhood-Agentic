# ADR-001 — The database is network-isolated and has no host port

**Status:** accepted · **Date:** 2026-07-28

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

- The egress block is real and verified: `wget http://1.1.1.1` from inside the container returns
  `Network unreachable`.
- Nothing off-box can reach the database, and neither can anything on the box that is not on
  `rh-internal` — a stronger posture than loopback binding, which is reachable by every local user.
- Containerizing the migration runner and the loader also **pins their Python to 3.12**, matching the
  deploy target. M's host Python is 3.14, so this closes part of P9 (the "passes locally ≠ passes in
  CI" gap) as a side effect rather than as separate work.

**Bad**

- Every host tool must go through a wrapper or a container. That is real friction, and friction
  invites shortcuts — hence `bin/db_psql.sh` existing up front rather than being left as an exercise.
- A Python process in the project venv cannot open a socket to the database. Anything needing direct
  access has to run containerized.

**Revisit if** the friction starts producing workarounds, or if a tool genuinely cannot be
containerized. The escape hatch is to move the DB to a non-internal bridge with loopback-only
publishing, accepting egress in exchange — at which point the least-privilege `rh_app` role (no
superuser, therefore no `COPY … FROM PROGRAM`) becomes the primary control rather than
defense-in-depth. That is a deliberate trade to make explicitly, not to drift into.
