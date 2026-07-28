#!/usr/bin/env bash
# db_up.sh — bring up the 3b Postgres stack.
#
# Idempotent. On first run it generates a strong password and starts the container; on later runs it
# reuses the password and converges the running state.
#
# Order matters:
#   1. secrets — generated and written entirely inside one python process: the password is created
#      with O_CREAT|O_EXCL at mode 0600 and never exists at a looser mode, never appears in any
#      process's argv (no second process ever holds it), and is never echoed
#   2. volume  — created explicitly (idempotent); the compose file declares it external so no
#      compose command can ever remove it
#   3. up      — compose reads credentials via --env-file; db/.env is parsed by compose's env-file
#      parser, NEVER `source`d as shell (a password containing `$(…)` or a space must stay a
#      password, not become a command)
#   4. wait    — block until the healthcheck actually passes; never report success on a dead DB
#
# There is no port step: the database is on an internal-only network and publishes no host port
# (ADR-001). Reach it with bin/db_psql.sh, or from a container attached to rh-internal. Note the
# password IS the on-box access control (the container's bridge IP is host-reachable, gated by
# scram auth — see docker-compose.db.yml), which is why the handling above is strict.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.db.yml"
PROJECT_NAME="rh-db"
CONTAINER_NAME="rh-db"
VOLUME_NAME="rh_db_data"

log() { echo "[db_up $(date -u +%H:%M:%S)] $*"; }
die() { echo "✗ $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker info >/dev/null 2>&1 || die "Docker daemon unreachable. On Linux: sudo systemctl start docker"

# ── 1. secrets ────────────────────────────────────────────────────────────────────────────────
if [[ ! -f "${DB_ENV}" ]]; then
  log "no db/.env — generating one with a fresh password"
  [[ -f "${PROJECT_DIR}/db/.env.example" ]] || die "db/.env.example missing"
  # One python process does everything: 32 URL-safe bytes from the CSPRNG, written through a file
  # descriptor opened O_CREAT|O_EXCL with mode 0600. The secret never reaches any argv (readable
  # by every uid via /proc/PID/cmdline) and the file is never group/world-readable, even between
  # two shell statements — an interrupt cannot strand it at a loose mode.
  python3 - "${PROJECT_DIR}/db/.env.example" "${DB_ENV}" <<'PY'
import os, secrets, sys

src, dst = sys.argv[1], sys.argv[2]
pw = secrets.token_urlsafe(32)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as out, open(src) as inp:
    for line in inp:
        out.write(f"POSTGRES_PASSWORD={pw}\n" if line.startswith("POSTGRES_PASSWORD=") else line)
PY
  log "wrote db/.env (created 0600) — password generated, not displayed"
else
  # A loose mode on a file holding the DB password is worth a loud warning, not a silent pass.
  mode="$(stat -c %a "${DB_ENV}")"
  [[ "${mode}" == "600" || "${mode}" == "400" ]] || log "⚠ db/.env is mode ${mode}; expected 600"
fi

# Fail fast on a broken/placeholder credentials file before compose produces a stranger error.
# grep, not `source`: the file is data, not shell.
for var in POSTGRES_USER POSTGRES_DB POSTGRES_PASSWORD; do
  grep -qE "^${var}=." "${DB_ENV}" || die "${var} missing from db/.env"
done
grep -qE '^POSTGRES_PASSWORD=replace-me$' "${DB_ENV}" \
  && die "db/.env still has the placeholder password — delete the file and re-run to generate one"

# ── 2. volume ─────────────────────────────────────────────────────────────────────────────────
# The compose file declares rh_db_data `external: true` so no `compose down -v` can remove it;
# the flip side is that something must create it, and that is here. Idempotent.
docker volume create "${VOLUME_NAME}" >/dev/null

# ── 3. up ─────────────────────────────────────────────────────────────────────────────────────
log "starting Postgres (internal network, no host port — see ADR-001)"
docker compose --env-file "${DB_ENV}" -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d

# ── 4. wait for health ────────────────────────────────────────────────────────────────────────
# Poll the container's own healthcheck rather than guessing with sleep. Fail fast if the container
# exits — a dead container will never become healthy, so waiting the full timeout is wasted.
log "waiting for healthcheck"
for _ in $(seq 1 60); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo missing)"
  state="$(docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo missing)"
  case "${status}" in
    healthy) log "✓ Postgres healthy. Connect with: bin/db_psql.sh"; exit 0 ;;
  esac
  case "${state}" in
    exited|dead) die "container ${CONTAINER_NAME} ${state}. Logs: docker logs ${CONTAINER_NAME}" ;;
  esac
  sleep 2
done

die "Postgres did not become healthy in 120s. Logs: docker logs ${CONTAINER_NAME}"
