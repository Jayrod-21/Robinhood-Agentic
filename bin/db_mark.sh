#!/usr/bin/env bash
# db_mark.sh — run db/mark_portfolios.py (the daily marking job, issue #36) against the 3b database.
#
# The database is on `rh-internal` with no host port (ADR-001), so the job runs from a container
# attached to that network — the same pattern as bin/db_migrate.sh and bin/db_load_bars.sh. The
# marking job reads and writes ONLY the database (prices are already loaded; nothing is fetched),
# so unlike db_corporate_actions.sh it never joins the egress network.
#
# Usage — every argument passes straight through to mark_portfolios.py:
#   bin/db_mark.sh live                                   # mark the latest session, kind='live'
#   bin/db_mark.sh live --date 2026-08-12
#   bin/db_mark.sh backfill --from 2024-05-01 --to 2024-06-28
#   bin/db_mark.sh backfill --from … --to … --portfolio 1 --dry-run
#
# Exit codes are the job's: 0 ok · 1 validation (coverage holes, drift, bad window) ·
# 2 SQL failure · 3 connection failure.

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
docker image inspect "${IMAGE}" >/dev/null 2>&1 \
  || die "image ${IMAGE} missing — run bin/db_migrate.sh status once to build it"

# Parsed, never sourced — see bin/lib_env.sh for the outage that made this a rule.
# shellcheck source=bin/lib_env.sh
source "${SCRIPT_DIR}/lib_env.sh"
load_env_file "${DB_ENV}"

: "${POSTGRES_USER:?POSTGRES_USER missing from db/.env}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing from db/.env}"
: "${POSTGRES_DB:?POSTGRES_DB missing from db/.env}"

# Assembled here and passed via the environment, never on the command line, so the password is not
# visible in `ps`. Derived from the primitives so a rotated password cannot drift from a stale copy.
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DB_HOST}:5432/${POSTGRES_DB}"

docker_flags=(
  --rm
  --network "${NETWORK}"
  --env DATABASE_URL
  --volume "${PROJECT_DIR}:/repo:ro"
  --entrypoint python
  # Run as the HOST user, matching the other wrappers: nothing here needs image-side privileges,
  # and the repo mount stays readable regardless of host permissions on data/.
  --user "$(id -u):$(id -g)"
  --init
)
[[ -t 0 ]] && docker_flags+=(--tty)

exec docker run "${docker_flags[@]}" "${IMAGE}" /repo/db/mark_portfolios.py "$@"
