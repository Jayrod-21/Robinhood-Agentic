#!/usr/bin/env bash
# db_psql.sh — open psql against the 3b database.
#
# The database is on an internal-only Docker network and publishes no host port (ADR-001), so this
# is the supported way to reach it interactively. `docker exec` also means the host needs no
# postgres-client installed, and the password never crosses the host command line or `ps` — psql
# reads it from the container's own environment.
#
# Usage:
#   bin/db_psql.sh                          # interactive shell
#   bin/db_psql.sh -c 'select 1'            # one-shot; any psql flags pass straight through
#   echo 'select 1' | bin/db_psql.sh -f -   # read SQL from stdin

set -Eeuo pipefail

CONTAINER_NAME="rh-db"

if ! docker inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
  echo "✗ container ${CONTAINER_NAME} does not exist. Start it: bin/db_up.sh" >&2
  exit 1
fi
if [[ "$(docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}")" != "running" ]]; then
  echo "✗ container ${CONTAINER_NAME} is not running. Start it: bin/db_up.sh" >&2
  exit 1
fi

# -it only when stdin is a TTY, so piping SQL in and capturing output both work unchanged.
docker_flags=(--interactive)
[[ -t 0 ]] && docker_flags+=(--tty)

# POSTGRES_USER/DB are already in the container's environment; PGPASSWORD is set from
# POSTGRES_PASSWORD inside the container so it is never an argument on the host side.
exec docker exec "${docker_flags[@]}" "${CONTAINER_NAME}" \
  sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' \
  -- "$@"
