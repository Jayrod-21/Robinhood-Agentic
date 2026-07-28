#!/usr/bin/env bash
# db_migrate.sh — run db/migrate.py against the 3b database.
#
# The database has no host port (ADR-001), so the runner executes inside a container attached to
# rh-internal. The repo is mounted READ-ONLY: the migration set that runs is the one in the working
# tree, and the runner cannot modify it.
#
# Usage — every argument passes straight through to migrate.py:
#   bin/db_migrate.sh status
#   bin/db_migrate.sh up --dry-run
#   bin/db_migrate.sh up
#   bin/db_migrate.sh up --allow-destructive
#   bin/db_migrate.sh down --allow-destructive --target 001
#
# Exit codes are the runner's: 0 ok · 1 validation · 2 SQL failure · 3 connection.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
IMAGE="rh-migrate:local"
NETWORK="rh-internal"
DB_HOST="rh-db"

die() { echo "✗ $*" >&2; exit 1; }

[[ -f "${DB_ENV}" ]] || die "db/.env missing — run bin/db_up.sh first"
docker network inspect "${NETWORK}" >/dev/null 2>&1 || die "network ${NETWORK} missing — run bin/db_up.sh first"
[[ "$(docker inspect --format '{{.State.Status}}' "${DB_HOST}" 2>/dev/null || echo missing)" == "running" ]] \
  || die "container ${DB_HOST} is not running — run bin/db_up.sh first"

# Build only when the image is absent or its inputs changed. `docker build` is already
# cache-efficient, but skipping the call entirely keeps `status` genuinely fast.
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "building ${IMAGE}…"
  docker build -q -f "${PROJECT_DIR}/db/Dockerfile" -t "${IMAGE}" "${PROJECT_DIR}/db" >/dev/null
fi

set -a
# shellcheck disable=SC1090
source "${DB_ENV}"
set +a

: "${POSTGRES_USER:?POSTGRES_USER missing from db/.env}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing from db/.env}"
: "${POSTGRES_DB:?POSTGRES_DB missing from db/.env}"

# DATABASE_URL is assembled here and passed via the environment, never on the command line, so the
# password is not visible in `ps`. Derived from the primitives rather than stored, so a rotated
# password cannot drift out of sync with a stale copy of the URL.
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DB_HOST}:5432/${POSTGRES_DB}"

docker_flags=(--rm --network "${NETWORK}" --env DATABASE_URL --volume "${PROJECT_DIR}:/repo:ro")
[[ -t 0 ]] && docker_flags+=(--tty)

exec docker run "${docker_flags[@]}" "${IMAGE}" "$@"
