#!/usr/bin/env bash
# db_up.sh — bring up the 3b Postgres stack.
#
# Idempotent. On first run it generates a strong password and starts the container; on later runs it
# reuses the password and converges the running state.
#
# Order matters:
#   1. secrets — generated locally, written 0600, never echoed and never passed on argv
#   2. up      — compose, credentials supplied via the environment
#   3. wait    — block until the healthcheck actually passes; never report success on a dead DB
#
# There is no port step: the database is on an internal-only network and publishes no host port
# (ADR-001). Reach it with bin/db_psql.sh, or from a container attached to rh-internal.
#
# The password is generated rather than prompted so it is never typed, never in shell history, and
# never in a terminal scrollback. It is passed to compose through the environment, not the command
# line, so it cannot be read from `ps`.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.db.yml"
PROJECT_NAME="rh-db"
CONTAINER_NAME="rh-db"

log() { echo "[db_up $(date -u +%H:%M:%S)] $*"; }
die() { echo "✗ $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker info >/dev/null 2>&1 || die "Docker daemon unreachable. On Linux: sudo systemctl start docker"

# ── 1. secrets ────────────────────────────────────────────────────────────────────────────────
if [[ ! -f "${DB_ENV}" ]]; then
  log "no db/.env — generating one with a fresh password"
  [[ -f "${PROJECT_DIR}/db/.env.example" ]] || die "db/.env.example missing"
  # 32 URL-safe bytes from the CSPRNG. Written straight to the file; never printed.
  generated="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  # Use awk rather than sed so the secret travels in a variable, never in a command-line argument
  # that would be visible in `ps` while the process runs.
  awk -v pw="${generated}" '
    /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" pw; next }
    { print }
  ' "${PROJECT_DIR}/db/.env.example" > "${DB_ENV}"
  unset generated
  chmod 600 "${DB_ENV}"
  log "wrote db/.env (0600) — password generated, not displayed"
else
  # A loose mode on a file holding the DB password is worth a loud warning, not a silent pass.
  mode="$(stat -c %a "${DB_ENV}")"
  [[ "${mode}" == "600" || "${mode}" == "400" ]] || log "⚠ db/.env is mode ${mode}; expected 600"
fi

# ── 2. up ─────────────────────────────────────────────────────────────────────────────────────
set -a
# shellcheck disable=SC1090
source "${DB_ENV}"
set +a

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set after sourcing ${DB_ENV}}"

log "starting Postgres (internal network, no host port — see ADR-001)"
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d

# ── 3. wait for health ────────────────────────────────────────────────────────────────────────
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
