#!/usr/bin/env bash
# scheduled_cycle.sh <open|close> [extra args passed to the cycle job]
#
# What cron runs twice a day. Two steps:
#   1. Host-side: refresh the account snapshot via the Robinhood MCP (bin/refresh_once.sh) — the MCP
#      lives on the host, not in the container.
#   2. In-container: run the cycle job (scan + per-position debates + report) which reads the fresh
#      snapshot from the shared volume.
#
# A failed refresh does NOT abort the cycle — it runs against the last good snapshot and logs a warning.
# Env knobs: AGENTIC_MCP_CWD (claude cwd for the MCP), AGENTIC_COMPOSE_FILE (compose file to exec into).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

phase="${1:-}"
if [[ "${phase}" != "open" && "${phase}" != "close" ]]; then
  echo "usage: scheduled_cycle.sh <open|close> [--max-debates N]" >&2
  exit 2
fi
shift || true

LOG_DIR="${PROJECT_DIR}/logs/cron"
mkdir -p "${LOG_DIR}"
exec >>"${LOG_DIR}/$(date -u +%Y%m%d)-${phase}.log" 2>&1

echo "=== scheduled_cycle ${phase} @ $(date -u +%FT%TZ) ==="

# 1. Refresh the snapshot (best-effort).
if bash "${SCRIPT_DIR}/refresh_once.sh"; then
  echo "snapshot refreshed"
else
  echo "WARN: snapshot refresh failed — running cycle on the last good snapshot"
fi

# 2. Run the cycle inside the backend container.
DOCKER="docker"
command -v docker >/dev/null 2>&1 || DOCKER="docker.exe"
COMPOSE_FILE="${AGENTIC_COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.yml}"
set -a; [ -f "${PROJECT_DIR}/.env.ports" ] && source "${PROJECT_DIR}/.env.ports"; set +a

cd "${PROJECT_DIR}" || exit 1
"${DOCKER}" compose -f "${COMPOSE_FILE}" exec -T backend python -m app.jobs.cycle "${phase}" "$@"

echo "=== done @ $(date -u +%FT%TZ) ==="
