#!/usr/bin/env bash
# db_backup.sh — dump the 3b database to data/backups/db/ and verify the dump is restorable-shaped.
#
# Bar §4.10: automated, tested backups — an untested backup is not a backup. This script:
#   1. pg_dump (custom format, compressed) via docker exec — no host postgres-client needed, and
#      the credential never crosses the host command line (in-container loopback is trust);
#   2. VERIFIES the dump by running pg_restore --list over it (catches truncated/corrupt output —
#      the failure mode a bare `pg_dump > file` never surfaces);
#   3. prunes to the newest KEEP_BACKUPS dumps (default 14).
#
# Scheduling is the operator's: `bash bin/db_backup.sh` from cron/systemd-timer daily is the
# intent. Off-host copies (Bar §4.10 "off-site") are out of scope here — data/ is on the same
# disk; treat these dumps as protection against bad migrations and fat-fingered deletes, not
# against disk loss.
#
# Exit codes: 0 ok · 1 any failure (dump, verify, or prune) — loud, never partial-success.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONTAINER_NAME="${RH_DB_CONTAINER:-rh-db}"
BACKUP_DIR="${PROJECT_DIR}/data/backups/db"
KEEP_BACKUPS="${KEEP_BACKUPS:-14}"

log() { echo "[db_backup $(date -u +%H:%M:%S)] $*"; }
die() { echo "✗ $*" >&2; exit 1; }

[[ "$(docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo missing)" == "running" ]] \
  || die "container ${CONTAINER_NAME} is not running — run bin/db_up.sh first"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"  # dumps contain everything the DB does; same posture as data/

stamp="$(date -u +%Y%m%d_%H%M%S)"
dump="${BACKUP_DIR}/rh_db_${stamp}.dump"
tmp="${dump}.partial"  # write to .partial and rename, so an interrupted dump never looks complete

log "dumping to ${dump}"
docker exec --user postgres "${CONTAINER_NAME}" \
  sh -c 'exec pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' \
  > "${tmp}" || { rm -f "${tmp}"; die "pg_dump failed"; }

[[ -s "${tmp}" ]] || { rm -f "${tmp}"; die "pg_dump produced an empty file"; }

# Restorability check: pg_restore parses the custom-format TOC end to end. A truncated or corrupt
# dump fails here, today, instead of during a real restore under pressure.
docker exec --interactive "${CONTAINER_NAME}" pg_restore --list < "${tmp}" > /dev/null \
  || { rm -f "${tmp}"; die "pg_restore --list rejected the dump — NOT keeping it"; }

mv "${tmp}" "${dump}"
log "✓ dump verified: $(du -h "${dump}" | cut -f1) $(basename "${dump}")"

# Retention: newest KEEP_BACKUPS survive. Names are our own UTC stamp format, so a lexical
# reverse sort IS newest-first — no mtime parsing needed.
prunable="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'rh_db_*.dump' -printf '%f\n' \
  | sort -r | tail -n "+$((KEEP_BACKUPS + 1))")"
if [[ -n "${prunable}" ]]; then
  while IFS= read -r old; do
    rm -f "${BACKUP_DIR}/${old}"
    log "pruned ${old}"
  done <<< "${prunable}"
fi

kept="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'rh_db_*.dump' | wc -l)"
log "✓ done (${kept} dump(s) retained, keep=${KEEP_BACKUPS})"
