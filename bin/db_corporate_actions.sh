#!/usr/bin/env bash
# db_corporate_actions.sh — fetch splits/dividends and populate adj_close.
#
# THE NETWORK PROBLEM THIS SOLVES
#   ADR-001 puts the database on `rh-internal`, an internal-only network with no egress, so a
#   container attached to it cannot reach a market-data provider. The host has the opposite problem:
#   internet, but no route to the database (there is no host port, deliberately).
#
#   This loader needs both. It gets them by attaching to `rh-internal` AND a second user-defined
#   bridge, `rh-egress`. That does NOT weaken the property ADR-001 exists to protect: `rh-db` itself
#   remains attached only to `rh-internal` and still cannot call out. Verified — a wget from inside
#   rh-db returns "Network unreachable" while this container reaches the internet fine.
#
#   The default `bridge` cannot be used for this: Docker refuses to mix a user-defined network with
#   a non-user-defined one.
#
# Usage:
#   bin/db_corporate_actions.sh fetch --candidates gaps    # ask the provider about anomalous names
#   bin/db_corporate_actions.sh fetch --symbols NVDA AAPL  # or specific ones
#   bin/db_corporate_actions.sh adjust                     # populate adj_close
#   bin/db_corporate_actions.sh verify                     # report gaps the pass did not explain

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
IMAGE="rh-actions:local"
DB_NET="rh-internal"
EGRESS_NET="rh-egress"
DB_HOST="rh-db"

die() { echo "✗ $*" >&2; exit 1; }

[[ -f "${DB_ENV}" ]] || die "db/.env missing — run bin/db_up.sh first"
docker network inspect "${DB_NET}" >/dev/null 2>&1 || die "network ${DB_NET} missing — run bin/db_up.sh first"
[[ "$(docker inspect --format '{{.State.Status}}' "${DB_HOST}" 2>/dev/null || echo missing)" == "running" ]] \
  || die "container ${DB_HOST} is not running — run bin/db_up.sh first"

# Idempotent: an existing network is left untouched. No host port is bound, so there is no port to
# collide with another stack on this machine.
docker network inspect "${EGRESS_NET}" >/dev/null 2>&1 || {
  echo "creating ${EGRESS_NET} (egress for the provider fetch; the DB never joins it)"
  docker network create "${EGRESS_NET}" >/dev/null
}

# A separate image from rh-migrate: this one needs yfinance, and the migration runner has no
# business carrying a market-data client or its transitive dependencies.
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "building ${IMAGE}…"
  docker build -q -f "${PROJECT_DIR}/db/Dockerfile.actions" -t "${IMAGE}" "${PROJECT_DIR}/db" >/dev/null
fi

set -a
# shellcheck disable=SC1090
source "${DB_ENV}"
set +a
: "${POSTGRES_USER:?missing from db/.env}"
: "${POSTGRES_PASSWORD:?missing from db/.env}"
: "${POSTGRES_DB:?missing from db/.env}"

# Assembled here and passed through the environment, never on the command line, so the password
# cannot be read from `ps`.
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DB_HOST}:5432/${POSTGRES_DB}"

flags=(
  --rm
  --network "${DB_NET}"
  --network "${EGRESS_NET}"
  --env DATABASE_URL
  --volume "${PROJECT_DIR}:/repo:ro"
  --user "$(id -u):$(id -g)"
  --env HOME=/tmp
  --init
)
# Pass FMP_API_KEY through only when the caller exported it (load_delistings.py fmp needs it).
# Via the environment, never argv, so the key cannot be read from `ps`.
[[ -n "${FMP_API_KEY:-}" ]] && flags+=(--env FMP_API_KEY)
[[ -t 0 ]] && flags+=(--tty)

# LOADER_SCRIPT lets this wrapper serve any loader that needs BOTH the database and egress.
SCRIPT="${LOADER_SCRIPT:-/repo/db/load_corporate_actions.py}"
exec docker run "${flags[@]}" "${IMAGE}" "${SCRIPT}" "$@"
