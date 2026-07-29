#!/usr/bin/env bash
# db_load_bars.sh — load Polygon minute bars into Postgres.
#
# The database has no host port (ADR-001), so the loader runs in a container attached to
# rh-internal, exactly like the migration runner. The repo is mounted READ-ONLY: the loader only
# reads `data/market/`, and mounting it writable would let a bug corrupt 23 GB of irreplaceable
# archive (the Bloomberg sample in particular cannot be re-pulled).
#
# Reuses the rh-migrate image — same Python 3.12, same pinned psycopg — with the entrypoint
# overridden. A second image for one more script would be two things to keep in sync.
#
# Usage — arguments pass through to db/load_minute_bars.py:
#   bin/db_load_bars.sh --dry-run --limit 3
#   bin/db_load_bars.sh --limit 5
#   bin/db_load_bars.sh                       # the full archive
#
# Paths are CONTAINER paths (the repo is at /repo), so --root defaults correctly but an explicit
# --root must be given as /repo/…
#
# Exit codes are the loader's: 0 ok · 1 validation · 2 SQL failure · 3 connection.

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

set -a
# shellcheck disable=SC1090
source "${DB_ENV}"
set +a

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
  # Run as the HOST user, not the image's uid 1001. data/market/ is 0700 owned by the host user —
  # deliberately, since data/ also holds the account snapshot — so uid 1001 cannot read the archive.
  # The migration runner never hit this because it only reads db/. Loosening the directory instead
  # would undo a security fix to save a flag.
  --user "$(id -u):$(id -g)"
  # A 2-billion-row load is long-running; without this a disconnected terminal kills it mid-file.
  # The per-file transaction means an interrupted run rolls back cleanly and resumes by hash.
  --init
)
[[ -t 0 ]] && docker_flags+=(--tty)

SCRIPT="${LOADER_SCRIPT:-/repo/db/load_minute_bars.py}"
exec docker run "${docker_flags[@]}" "${IMAGE}" "${SCRIPT}" "$@"
