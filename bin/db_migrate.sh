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
# Exit codes are the runner's: 0 ok · 1 validation (incl. CLI usage errors) · 2 SQL failure ·
# 3 connection.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
IMAGE="rh-migrate:local"
NETWORK="rh-internal"
DB_HOST="rh-db"

die() { echo "✗ $*" >&2; exit 1; }

# read_env_value <key> — strict parse of db/.env. The file is DATA: `source` would execute it as
# shell, turning a password containing `$(…)`, a backtick, or a space into command execution or a
# silently truncated credential. grep+cut cannot execute anything.
read_env_value() {
  local line
  line="$(grep -m1 -E "^$1=" "${DB_ENV}")" || return 1
  printf '%s' "${line#*=}"
}

[[ -f "${DB_ENV}" ]] || die "db/.env missing — run bin/db_up.sh first"
docker network inspect "${NETWORK}" >/dev/null 2>&1 || die "network ${NETWORK} missing — run bin/db_up.sh first"
[[ "$(docker inspect --format '{{.State.Status}}' "${DB_HOST}" 2>/dev/null || echo missing)" == "running" ]] \
  || die "container ${DB_HOST} is not running — run bin/db_up.sh first"

# Validate credentials BEFORE the build step: a missing variable should cost one grep, not an
# image build.
POSTGRES_USER="$(read_env_value POSTGRES_USER)" || die "POSTGRES_USER missing from db/.env"
POSTGRES_PASSWORD="$(read_env_value POSTGRES_PASSWORD)" || die "POSTGRES_PASSWORD missing from db/.env"
POSTGRES_DB="$(read_env_value POSTGRES_DB)" || die "POSTGRES_DB missing from db/.env"

# Build when the image is absent OR its inputs changed. "Inputs changed" is real, not aspirational:
# the build inputs (Dockerfile + requirements.txt) are hashed into an image label, and a mismatch
# triggers a rebuild — bumping the psycopg pin cannot be silently ignored. migrate.py itself is not
# an input: it is bind-mounted at run time, never baked in.
inputs_sha="$(cat "${PROJECT_DIR}/db/Dockerfile" "${PROJECT_DIR}/db/requirements.txt" | sha256sum | cut -d' ' -f1)"
current_sha="$(docker image inspect --format '{{index .Config.Labels "rh.build_inputs_sha256"}}' "${IMAGE}" 2>/dev/null || true)"
if [[ "${current_sha}" != "${inputs_sha}" ]]; then
  echo "building ${IMAGE}…"
  docker build -q -f "${PROJECT_DIR}/db/Dockerfile" \
    --label "rh.build_inputs_sha256=${inputs_sha}" \
    -t "${IMAGE}" "${PROJECT_DIR}/db" >/dev/null
fi

# libpq-style PG* variables, passed by NAME (--env with no value): the credential never appears in
# any argv, and no URL is ever assembled — a password containing '@', '/', or '%' would redirect or
# break a concatenated postgresql:// URL (psycopg parses the userinfo section by delimiter), and
# percent-encoding by hand is exactly the kind of correctness nobody re-verifies. libpq reads these
# directly with no parsing layer in between. Values derive from db/.env on every run, so a rotated
# password cannot drift out of sync with a stale copy.
export PGHOST="${DB_HOST}"
export PGPORT=5432
export PGUSER="${POSTGRES_USER}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
export PGDATABASE="${POSTGRES_DB}"

docker_flags=(--rm --network "${NETWORK}"
  --env PGHOST --env PGPORT --env PGUSER --env PGPASSWORD --env PGDATABASE
  --volume "${PROJECT_DIR}:/repo:ro")
[[ -t 0 ]] && docker_flags+=(--tty)

exec docker run "${docker_flags[@]}" "${IMAGE}" "$@"
