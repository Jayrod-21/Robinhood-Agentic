#!/usr/bin/env bash
# refresh_once.sh — one headless Robinhood snapshot refresh. Server-friendly: no wt.exe, no daemon.
#
# Runs `claude --print` with the robinhood-trading MCP, pre-authorizing ONLY the two read-only
# account pulls + Write + `date` (no order tool, no broad Bash). Writes data/account_snapshot.json.
# This is the canonical refresh used by BOTH the cron cycle (bin/scheduled_cycle.sh) and the
# Refresh-button daemon (bin/refresh_daemon.sh) on a headless host.
#
# Preconditions on the host: Claude Code installed + logged in, and the robinhood-trading MCP added
# and OAuth-authenticated (ideally at USER scope so it's reachable from any cwd). See SERVER_DEPLOY.md.
#
# Exit: 0 = snapshot rewritten (mtime advanced); 1 = ran but snapshot unchanged; 2 = setup error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SNAPSHOT_FILE="${PROJECT_DIR}/data/account_snapshot.json"
PROMPT_FILE="${SCRIPT_DIR}/refresh_prompt.md"
LOG_DIR="${PROJECT_DIR}/logs/refresh"

# Directory to run `claude` from so the robinhood MCP loads. With a USER-scoped MCP any dir works;
# with a project-scoped MCP, point this at that project root. Override via AGENTIC_MCP_CWD.
MCP_CWD="${AGENTIC_MCP_CWD:-${PROJECT_DIR}}"
TIMEOUT="${AGENTIC_REFRESH_TIMEOUT:-150}"

ALLOWED_TOOLS=(
  mcp__robinhood-trading__get_portfolio
  mcp__robinhood-trading__get_equity_positions
  Write
  'Bash(date*)'
)

mkdir -p "${LOG_DIR}"
ts() { date -u +%H:%M:%S; }
log() { echo "[refresh-once $(ts)] $*"; }

claude_bin() {
  if command -v claude >/dev/null 2>&1; then command -v claude; return; fi
  # ${HOME} only — the retired WSL box's /root/.nvm path is dead for every other operator.
  for c in "${AGENTIC_CLAUDE_BIN:-}" "${HOME}/.nvm/versions/node/"*/bin/claude; do
    [[ -x "${c}" ]] && { echo "${c}"; return; }
  done
  echo ""
}

snapshot_mtime() { stat -c %Y "${SNAPSHOT_FILE}" 2>/dev/null || echo 0; }

cb="$(claude_bin)"
if [[ -z "${cb}" ]]; then
  log "ERROR: claude CLI not found on PATH. Install Claude Code on this host."
  exit 2
fi
if [[ ! -f "${PROMPT_FILE}" ]]; then
  log "ERROR: prompt file missing: ${PROMPT_FILE}"
  exit 2
fi

runlog="${LOG_DIR}/once-$(date -u +%Y%m%dT%H%M%SZ).log"
before="$(snapshot_mtime)"
log "pulling Agentic account via MCP (cwd=${MCP_CWD}, timeout=${TIMEOUT}s) → ${runlog}"

# Prompt is the positional arg FIRST; the variadic --allowedTools comes last so it can't swallow it.
( cd "${MCP_CWD}" && timeout "${TIMEOUT}" "${cb}" --print "$(cat "${PROMPT_FILE}")" \
    --allowedTools "${ALLOWED_TOOLS[@]}" ) > "${runlog}" 2>&1 || true

after="$(snapshot_mtime)"
if (( after > before )); then
  # The snapshot is written by the MCP session's Write tool, which uses the ambient umask — that
  # yields 0664 on this host. It holds cash, buying power, and every position with cost basis, so
  # restrict it explicitly rather than relying on data/ being 0700 to hide it. Also applies to the
  # run log, which echoes the same figures.
  chmod 600 "${SNAPSHOT_FILE}" "${runlog}" 2>/dev/null || true
  log "✓ snapshot refreshed"
  exit 0
fi
log "✗ snapshot NOT updated — check ${runlog} (MCP auth may have expired; re-auth over SSH)."
exit 1
