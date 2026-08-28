#!/usr/bin/env bash
# lib_env.sh — load a KEY=VALUE env file WITHOUT letting the shell evaluate it.
#
# WHY THIS EXISTS
#   Every script here used `set -a; source "$ENV_FILE"; set +a`. bin/db_migrate.sh has warned since
#   it was written that this is wrong — "the file is DATA: `source` would execute it as shell,
#   turning a password containing $(…), a backtick, or a space into command execution or a silently
#   truncated credential" — and six scripts did it anyway.
#
#   It came due on 2026-08-26, when owner labels were added to backend/.env for the LLM cost split:
#
#       ANTHROPIC_API_KEY_NAME=Jared Anthropic
#       backend/.env: line 7: Anthropic: command not found     (exit 127)
#
#   Bash read that as "set NAME=Jared, then run the command `Anthropic`". Under `set -e` the script
#   died there. alpaca_sync.sh stopped writing the fallback snapshot, nightly_marks.sh stopped
#   loading daily bars and scoring judgments, and both failed BEFORE reaching their own
#   failure-reporting code — so neither announced it. Nothing in the value was malicious. A space
#   was enough.
#
# WHY NOT JUST QUOTE THE VALUES
#   Because it would fix the shell and break the containers. `docker run --env-file` does NOT strip
#   quotes — measured: `X="hello world"` arrives as the literal `"hello world"`, quotes included.
#   Quoting the owner labels would have created a second, differently-named owner in llm_usage and
#   silently split the cost ledger in half. The file has to stay unquoted, so the readers have to
#   stop evaluating it.
set -uo pipefail

# load_env_file <path> — export every KEY=VALUE, value verbatim to end of line.
#
# No expansion, no word splitting, no command substitution. Keys that are not valid shell
# identifiers are skipped rather than silently mangled. A missing file is the caller's business:
# this returns 1 and says nothing, because "no .env" is fatal for some callers and normal for others.
load_env_file() {
  local path="$1" line key value
  [[ -f "${path}" ]] || return 1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    [[ "${line}" =~ ^[[:space:]]*$ ]] && continue
    [[ "${line}" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    # Trailing whitespace on the KEY is a typo; leading/trailing whitespace in the VALUE is data
    # and is preserved, because a credential could legitimately contain it.
    key="${key//[[:space:]]/}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "${key}=${value}"
  done < "${path}"
  return 0
}
