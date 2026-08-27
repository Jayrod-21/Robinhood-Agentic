#!/usr/bin/env bash
# scheduled_cycle.sh <open|close> [extra args passed to the cycle job]
#
# What cron runs each morning. Two steps:
#   1. Host-side: refresh the FALLBACK account snapshot from Alpaca (bin/alpaca_snapshot.py). This
#      used to pull from the Robinhood MCP, which lived on the host and shelled out to wt.exe — a
#      WSL-era path that cannot work on this machine. The broker is Alpaca and the container reads
#      it live; this only keeps the fallback file current.
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

# shellcheck source=bin/lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

LOG_DIR="${PROJECT_DIR}/logs/cron"
mkdir -p "${LOG_DIR}"
exec >>"${LOG_DIR}/$(date -u +%Y%m%d)-${phase}.log" 2>&1

echo "=== scheduled_cycle ${phase} @ $(date -u +%FT%TZ) ==="

# 1. Refresh the fallback snapshot (best-effort — the container reads the broker live regardless).
if bash "${SCRIPT_DIR}/alpaca_sync.sh"; then
  echo "fallback snapshot refreshed"
else
  echo "WARN: fallback snapshot refresh failed — the live broker read is unaffected"
fi

# 2. Run the cycle inside the backend container.
#
# Defaults to the PROD compose file. This used to default to docker-compose.yml — the DEV stack —
# so a cron run would either hit a container that is not the deployed one or fail outright, and the
# override could not be set from a crontab because cron does not expand $HOME in its environment
# lines. Prod is what runs on a schedule; a dev run sets AGENTIC_COMPOSE_FILE explicitly.
COMPOSE_FILE="${AGENTIC_COMPOSE_FILE:-${PROJECT_DIR}/deploy/docker-compose.prod.yml}"
set -a; [ -f "${PROJECT_DIR}/.env.ports" ] && source "${PROJECT_DIR}/.env.ports"; set +a

cd "${PROJECT_DIR}" || exit 1

# The `docker.exe` fallback that used to be here was for WSL, where this project no longer runs.
rc=0
docker compose -f "${COMPOSE_FILE}" exec -T backend python -m app.jobs.cycle "${phase}" "$@" || rc=$?

if [[ ${rc} -ne 0 ]]; then
  # The exit code used to be swallowed by the trailing echo, so a cycle that never ran looked
  # exactly like one that did — and on this machine cron discards output, so nothing said otherwise.
  notify_transition "cycle-${phase}" "fail" "3b ${phase} cycle" \
    "the cycle did not complete (exit ${rc}); see ${LOG_DIR}"
  echo "✗ cycle ${phase} failed (exit ${rc})"
  exit "${rc}"
fi

notify_transition "cycle-${phase}" "ok" "3b ${phase} cycle" "completed"
echo "=== done @ $(date -u +%FT%TZ) ==="
