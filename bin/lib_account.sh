#!/usr/bin/env bash
# lib_account.sh — resolve the brokerage account number from configuration, never from source.
#
# WHY THIS EXISTS
#   The account number used to be written literally into bin/morning_scan.sh, bin/refresh_prompt.md
#   and the scan logs. It is an identifier rather than a credential — it cannot move money on its
#   own — but it is still a real brokerage account number sitting in cleartext in a repository that
#   is now shared, and it is about to become simply WRONG: a different account is being attached.
#   A value that is both sensitive-ish and short-lived belongs in configuration.
#
#   Config lives in backend/.env (gitignored; see backend/.env.example). Nothing here reads or
#   echoes the value beyond passing it to the prompt it belongs in.
#
# USAGE
#   source "${SCRIPT_DIR}/lib_account.sh"
#   require_account_number                      # sets AGENTIC_ACCOUNT_NUMBER or exits 2
#   rendered="$(render_prompt "${PROMPT_FILE}")"  # prints a temp path; caller traps cleanup

# Loads backend/.env (if present) and hard-requires AGENTIC_ACCOUNT_NUMBER.
# Exits 2 with an actionable message rather than proceeding with an empty account, which would
# otherwise reach the broker API as a malformed request and read as an auth problem.
require_account_number() {
  local project_dir env_file
  project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  env_file="${project_dir}/backend/.env"

  if [[ -z "${AGENTIC_ACCOUNT_NUMBER:-}" && -f "${env_file}" ]]; then
    # Only pull the one key: sourcing the whole file would also import the API key into the
    # environment of a process that has no business holding it.
    local from_file
    from_file="$(grep -E '^[[:space:]]*AGENTIC_ACCOUNT_NUMBER=' "${env_file}" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
    [[ -n "${from_file}" ]] && AGENTIC_ACCOUNT_NUMBER="${from_file}"
  fi

  if [[ -z "${AGENTIC_ACCOUNT_NUMBER:-}" ]]; then
    echo "ERROR: AGENTIC_ACCOUNT_NUMBER is not set." >&2
    echo "  Set it in ${env_file} (see backend/.env.example) or export it before running." >&2
    echo "  It is deliberately not stored in the repository." >&2
    return 2
  fi
  export AGENTIC_ACCOUNT_NUMBER
  return 0
}

# Renders a prompt template, replacing __AGENTIC_ACCOUNT_NUMBER__ with the configured value.
# Prints the path of a 0600 temp file. The caller is responsible for removing it.
render_prompt() {
  local src="$1" dest
  dest="$(mktemp /tmp/agentic-prompt-XXXXXX.md)"
  chmod 600 "${dest}"
  # The account number is a plain digit string, so a literal sed replacement is safe here.
  sed "s/__AGENTIC_ACCOUNT_NUMBER__/${AGENTIC_ACCOUNT_NUMBER}/g" "${src}" > "${dest}"
  echo "${dest}"
}
