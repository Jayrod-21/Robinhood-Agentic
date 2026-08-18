#!/usr/bin/env bash
# alpaca_sync.sh — keep the FALLBACK account snapshot current. Runs every minute from cron.
#
# WHAT THIS REPLACED
#   The "Refresh snapshot" button, and the host daemon behind it. That daemon pulled holdings from
#   the Robinhood MCP and, when the headless pull failed, opened a Windows Terminal tab via wt.exe —
#   machinery from when this project lived under WSL. On native Linux that path could not run at
#   all, so the fallback file sat at a July Robinhood export: 8 positions from a book that no longer
#   exists. Had Alpaca gone unreachable, the dashboard would have served that as truth.
#
# WHAT IT DOES NOT DO
#   It does not make the dashboard fresher. With credentials set, services/broker.py reads Alpaca on
#   every request behind a 5-second cache and never opens this file. The file matters only when the
#   broker does not answer — which is exactly when nobody can go fix it by hand.
#
# QUIET BY DESIGN
#   Cron mails any output. At once a minute, a chatty success line is 1,440 mails a day, and the
#   real signal drowns. Success is silent; only failure speaks, and failure leaves the previous
#   snapshot in place rather than blanking it.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

BACKEND_ENV="${PROJECT_DIR}/backend/.env"
[[ -f "${BACKEND_ENV}" ]] || { echo "✗ ${BACKEND_ENV} missing — no broker credentials" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "${BACKEND_ENV}"
set +a

PYTHON="${PROJECT_DIR}/.venv/bin/python"
[[ -x "${PYTHON}" ]] || { echo "✗ ${PYTHON} missing — create the venv (make venv)" >&2; exit 1; }

LOG_DIR="${PROJECT_DIR}/logs/refresh"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/$(date -u +%Y%m%d)-snapshot.log"

# Runs on the host, not in a container: this needs the broker and a file, never the database. A
# container spawn every 60 seconds to write one JSON file would be the most expensive part of it.
if out="$("${PYTHON}" "${SCRIPT_DIR}/alpaca_snapshot.py" 2>&1)"; then
  echo "$(date -u +%FT%TZ) ${out}" >>"${LOG}"
  exit 0
fi
rc=$?
echo "$(date -u +%FT%TZ) FAILED rc=${rc} ${out}" >>"${LOG}"
echo "alpaca_sync failed (rc=${rc}); see ${LOG}" >&2
exit "${rc}"
