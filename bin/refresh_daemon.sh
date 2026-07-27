#!/usr/bin/env bash
# refresh_daemon.sh — HOST-side bridge between the dockerized dashboard and the Robinhood MCP.
#
# Why this exists: the dashboard runs inside Docker and cannot reach the robinhood-trading MCP
# (an OAuth server scoped to this Claude session on the host). So the Refresh button only drops a
# trigger file on the shared ./data volume; THIS daemon — running on the WSL host, outside Docker —
# watches for that trigger and does the privileged pull via a real `claude` process that has the MCP.
#
# Flow per trigger (data/refresh.request):
#   1. Try a HEADLESS `claude --print` pull (silent, fast) with a timeout. If the OAuth token is
#      valid headless, the snapshot's generated_at advances and we're done — no window.
#   2. If headless didn't update the snapshot, open a VISIBLE Windows Terminal tab running `claude`
#      so the user can see it connect (and complete the Robinhood OAuth re-auth if prompted).
#   3. Remove the trigger once the snapshot updates (or after a timeout).
#
# wt.exe gotcha: never put ';' or ',' in wt.exe's argument list (it splits on them and spawns extra
# tabs). We hand wt.exe a fixed `wsl.exe bash <runner>` and keep all real logic inside the runner.
#
# Usage: bin/refresh_daemon.sh   (started in the background by bin/up.sh; Ctrl-C or kill to stop)

# -u catches unset vars; -o pipefail surfaces a broken stage in a pipe (e.g. the `... | tee` in
# open_visible_tab). -e is intentionally OMITTED: this is a long-running watch loop and must NOT die
# on the first non-zero exit (a missing snapshot `stat`, a `timeout` expiry, a failed `claude`).
# Those cases are handled explicitly via `|| true` guards and the status codes consumed at the loop.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/data"
REQUEST_FILE="${DATA_DIR}/refresh.request"
SNAPSHOT_FILE="${DATA_DIR}/account_snapshot.json"
PROMPT_FILE="${SCRIPT_DIR}/refresh_prompt.md"
LOG_DIR="${PROJECT_DIR}/logs/refresh"

# Directory to run `claude` from so the robinhood MCP loads. With a USER-scoped MCP (the documented
# setup — `claude mcp add --scope user …`) any directory works, so the project root is the safe
# default. Override via AGENTIC_MCP_CWD only if the MCP is project-scoped somewhere else.
#
# This used to be hard-coded to /root/Jared, the projects root on the retired WSL box. On any other
# host that path does not exist (or is not readable), so the `cd` below failed and every headless
# refresh exited 1 — which looks identical to "the MCP isn't authenticated".
MCP_CWD="${AGENTIC_MCP_CWD:-${PROJECT_DIR}}"

POLL_INTERVAL=2          # seconds between trigger checks
HEADLESS_TIMEOUT=100     # seconds for the silent attempt
TAB_TIMEOUT=240          # seconds to wait for the visible tab to update the snapshot
# On WSL/Windows default to a VISIBLE wt.exe tab (the user watches it connect). On a headless host
# (Ubuntu server — no wt.exe) default to a silent headless pull. Override with AGENTIC_REFRESH_HEADLESS.
if [[ -n "${AGENTIC_REFRESH_HEADLESS:-}" ]]; then
  ALLOW_HEADLESS="${AGENTIC_REFRESH_HEADLESS}"
elif command -v wt.exe >/dev/null 2>&1; then
  ALLOW_HEADLESS=0
else
  ALLOW_HEADLESS=1
fi

# Exact, least-privilege tools the refresh claude may use without an interactive approval prompt:
# the two read-only Robinhood pulls, Write (snapshot file), and `date` for the timestamp. No order
# placement tool is allowed. `--dangerously-skip-permissions` is intentionally NOT used (it is hard
# -blocked under root); pre-authorizing these specific tools is the root-safe, least-privilege path.
ALLOWED_TOOLS=(
  mcp__robinhood-trading__get_portfolio
  mcp__robinhood-trading__get_equity_positions
  Write
  'Bash(date*)'
)

mkdir -p "${LOG_DIR}"

log() { echo "[refresh-daemon $(date -u +%H:%M:%S)] $*"; }

claude_bin() {
  if command -v claude >/dev/null 2>&1; then command -v claude
  elif [[ -x /root/.nvm/versions/node/v22.20.0/bin/claude ]]; then echo /root/.nvm/versions/node/v22.20.0/bin/claude
  else echo ""; fi
}

snapshot_mtime() { stat -c %Y "${SNAPSHOT_FILE}" 2>/dev/null || echo 0; }

# Wait until the snapshot file's mtime advances past $1, or $2 seconds elapse. Returns 0 on update.
wait_for_update() {
  local baseline="$1" timeout="$2" waited=0
  while (( waited < timeout )); do
    if (( $(snapshot_mtime) > baseline )); then return 0; fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

# Headless silent pull — delegates to the canonical bin/refresh_once.sh so there's ONE refresh
# implementation. Returns 0 if the snapshot was updated.
try_headless() {
  local baseline="$1"
  log "headless pull via refresh_once.sh"
  AGENTIC_REFRESH_TIMEOUT="${HEADLESS_TIMEOUT}" AGENTIC_MCP_CWD="${MCP_CWD}" \
    bash "${SCRIPT_DIR}/refresh_once.sh" >/dev/null 2>&1 || true
  wait_for_update "${baseline}" 2
}

# Visible Windows Terminal tab running an interactive-capable claude.
open_visible_tab() {
  local cb runner runlog title
  cb="$(claude_bin)"
  if [[ -z "${cb}" ]]; then log "ERROR: claude CLI not found; cannot refresh"; return 1; fi
  runlog="${LOG_DIR}/tab-$(date -u +%Y%m%dT%H%M%SZ).log"
  runner="$(mktemp /tmp/agentic-refresh-XXXXXX.sh)"
  title="RH Refresh"

  # Shell-quote the allowed-tools list so it survives embedding in the runner script.
  local allowed_quoted
  allowed_quoted="$(printf '%q ' "${ALLOWED_TOOLS[@]}")"

  # All real logic lives in this runner (keeps semicolons out of wt.exe's argv). Prompt is the
  # positional arg FIRST; the variadic --allowedTools comes last so it doesn't swallow the prompt.
  cat > "${runner}" <<RUNNER
#!/usr/bin/env bash
# Self-delete this temp runner now (before the blocking read) so it's cleaned up even if the user
# leaves the tab open — the script is already loaded into memory, so removing the file is safe.
rm -f -- "\$0"
cd "${MCP_CWD}" || exit 1
echo "=== Robinhood Agentic refresh — connecting MCP, pulling account ==="
"${cb}" --print "\$(cat "${PROMPT_FILE}")" --allowedTools ${allowed_quoted} 2>&1 | tee "${runlog}"
echo
echo "=== refresh tab done — snapshot rewritten if you saw DONE above ==="
read -p "Press Enter to close..."
RUNNER
  chmod +x "${runner}"

  if grep -qi microsoft /proc/version 2>/dev/null && command -v wt.exe >/dev/null 2>&1; then
    log "opening Windows Terminal tab for visible refresh"
    wt.exe -w 0 nt --title "${title}" wsl.exe bash "${runner}" >/dev/null 2>&1 &
    disown 2>/dev/null || true
  else
    log "no wt.exe; running refresh inline"
    bash "${runner}" &
    disown 2>/dev/null || true
  fi
}

process_request() {
  local baseline updated=1
  baseline="$(snapshot_mtime)"
  log "refresh requested → pulling Agentic account via MCP"

  if [[ "${ALLOW_HEADLESS}" == "1" ]] && try_headless "${baseline}"; then
    log "headless pull updated the snapshot ✓"
    updated=0
  else
    [[ "${ALLOW_HEADLESS}" == "1" ]] && log "headless did not update snapshot — falling back to visible tab"
    open_visible_tab
    if wait_for_update "${baseline}" "${TAB_TIMEOUT}"; then
      log "visible tab updated the snapshot ✓"; updated=0
    else
      log "snapshot not updated within ${TAB_TIMEOUT}s (left trigger cleared anyway)"
    fi
  fi

  rm -f "${REQUEST_FILE}"
  return "${updated}"
}

log "watching ${REQUEST_FILE} (headless=${ALLOW_HEADLESS}); Ctrl-C to stop"
trap 'log "stopping"; exit 0' INT TERM
while true; do
  if [[ -f "${REQUEST_FILE}" ]]; then
    process_request || true
  fi
  sleep "${POLL_INTERVAL}"
done
