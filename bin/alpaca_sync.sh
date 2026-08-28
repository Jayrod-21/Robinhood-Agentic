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
# WHERE FAILURES GO
#   NOT to cron mail. This machine has no MTA and /var/mail is empty, so anything cron captures is
#   discarded — an earlier version of this comment claimed otherwise and was simply wrong. Failures
#   go through bin/lib_notify.sh: a log file, plus a desktop notification on the transition into
#   failure and again on recovery. Repeats stay quiet, because a popup every minute during an
#   outage is how someone learns to ignore popups.
#
#   A failure also leaves the previous snapshot in place rather than blanking it.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}" || exit 1

# shellcheck source=bin/lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

BACKEND_ENV="${PROJECT_DIR}/backend/.env"
[[ -f "${BACKEND_ENV}" ]] || { echo "✗ ${BACKEND_ENV} missing — no broker credentials" >&2; exit 1; }
# Parsed, never sourced — see bin/lib_env.sh for the outage that made this a rule.
# shellcheck source=bin/lib_env.sh
source "${SCRIPT_DIR}/lib_env.sh"
load_env_file "${BACKEND_ENV}"

PYTHON="${PROJECT_DIR}/.venv/bin/python"
[[ -x "${PYTHON}" ]] || { echo "✗ ${PYTHON} missing — create the venv (make venv)" >&2; exit 1; }

LOG_DIR="${PROJECT_DIR}/logs/refresh"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/$(date -u +%Y%m%d)-snapshot.log"

# Runs on the host, not in a container: this needs the broker and a file, never the database. A
# container spawn every 60 seconds to write one JSON file would be the most expensive part of it.
if out="$("${PYTHON}" "${SCRIPT_DIR}/alpaca_snapshot.py" 2>&1)"; then
  echo "$(date -u +%FT%TZ) ${out}" >>"${LOG}"
  notify_transition "alpaca-sync" "ok" "3b fallback snapshot" "writing normally"
  exit 0
fi
rc=$?
echo "$(date -u +%FT%TZ) FAILED rc=${rc} ${out}" >>"${LOG}"
notify_transition "alpaca-sync" "fail" "3b fallback snapshot" \
  "not updating (rc=${rc}) — the fallback is going stale; see ${LOG}"
echo "alpaca_sync failed (rc=${rc}); see ${LOG}" >&2
exit "${rc}"
