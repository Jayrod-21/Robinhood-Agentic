#!/usr/bin/env bash
# repo_hygiene.sh — fast, dependency-free hygiene gate over TRACKED files (issue #15).
#
# Four checks, each with a history in this repo:
#   1. account-number  — an unmasked 9-digit brokerage account number adjacent to the word
#                        "account". The literal number was scrubbed and replaced with the
#                        __AGENTIC_ACCOUNT_NUMBER__ placeholder rendered from config; this stops
#                        it (or any successor account) from coming back.
#   2. personal-path   — /root/Jared, /home/jared*, and similar absolute personal home paths.
#                        A pinned baseline of pre-existing historical references is allowed
#                        (shrink-only ratchet); any NEW file fails.
#   3. dangling-symlink — a tracked symlink whose target would not exist in a fresh clone. This
#                        repo once shipped a symlink to a gitignored file outside the repo, which
#                        dangled for everyone else.
#   4. tracked-env     — any .env file becoming tracked. Only .env.example is allowed.
#
# Runs identically in CI and locally: `bash .github/scripts/repo_hygiene.sh` from anywhere inside
# the repo. Exit 0 = clean, exit 1 = at least one violation (each printed with the offending file).

set -Eeuo pipefail

cd "$(git rev-parse --show-toplevel)"
REPO_ROOT="$(pwd)"

FAILURES=0

fail() {
  printf '\033[31mFAIL\033[0m %s\n' "$1"
  FAILURES=1
}

pass() {
  printf '\033[32mPASS\033[0m %s\n' "$1"
}

# This script (and this script only) is excluded from the content greps: it necessarily names the
# very patterns it hunts for.
readonly SELF=":(exclude).github/scripts/repo_hygiene.sh"

# ── 1. Unmasked brokerage account number ────────────────────────────────────────────────────────
# Exactly nine consecutive digits (bounded by non-digits, so epochs and hashes with longer runs
# don't trip it) within 40 characters of the word "account" on the same line, either side,
# case-insensitive. The rendered placeholder __AGENTIC_ACCOUNT_NUMBER__ contains no digits and
# never matches — the check cannot regress the scrub that introduced it.
# Captured third-party API payloads are excluded, and ONLY these. A financial statement is wall to
# wall "accountsPayables": 902000000 — the word "account" beside nine digits is its normal content,
# so leaving them in means the gate cries wolf on every fixture refresh until someone silences it
# for good. The exclusion is narrow and carries a standing condition:
#
#   FIXTURES MUST COME FROM ENDPOINTS THAT TAKE NO ACCOUNT CONTEXT.
#
# Everything under here is public-company reference data (AAPL fundamentals) fetched by symbol. A
# fixture captured from an account-scoped or authenticated endpoint could carry a real account
# number past this gate, so it does not belong in this directory — put it somewhere the gate still
# reads, or mask it before committing.
readonly VENDOR_FIXTURES=":(exclude)tests/fixtures/fmp/*.json"

check_account_number() {
  local hits
  hits="$(git grep -I -n -i -E \
    'account[^0-9]{0,40}[0-9]{9}([^0-9]|$)|(^|[^0-9])[0-9]{9}[^0-9]{0,40}account' \
    -- . "${SELF}" "${VENDOR_FIXTURES}" || true)"
  if [[ -n "${hits}" ]]; then
    fail "account-number: unmasked 9-digit account number found in tracked files:"
    printf '%s\n' "${hits}"
    printf '  → replace the literal with the __AGENTIC_ACCOUNT_NUMBER__ placeholder rendered from config, or mask it (e.g. ****4025).\n'
  else
    pass "account-number: no unmasked account numbers in tracked files"
  fi
}

# ── 2. Absolute personal home paths ─────────────────────────────────────────────────────────────
# Baseline: files that already contained historical references when this gate was added
# (2026-08-13) and are quoting the retired machine's layout, not using it. SHRINK-ONLY: remove
# entries as the files are cleaned; never add one. A match in any file NOT listed here fails.
readonly PERSONAL_PATH_BASELINE=(
  "bin/refresh_daemon.sh"                         # comment explaining the old hard-coded path was removed
  "docs/SECURITY_FINDINGS_2026-07-27.md"          # quotes the finding it reports
  "docs/fixpass/FIX_REPORT_2026-06-16_dashboard.md"
  "docs/fixpass/REVIEW_debate_engine_2026-06-16.md"
  "docs/fixpass/REVIEW_infra_bridge_2026-06-16.md"
  "docs/fixpass/REVIEW_migrate_runner_2026-07-28.md"
  "logs/scans/cron.log"                           # historical log lines from the old machine
)

check_personal_paths() {
  local pattern='(/root/[Jj]ared|/home/jared[a-z._-]*|/[Uu]sers/[Jj]ared|[Cc]:\\+[Uu]sers\\+[Jj]ared)'
  local hits
  hits="$(git grep -I -n -E "${pattern}" -- . "${SELF}" || true)"

  local new_hits=""
  local line file
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    file="${line%%:*}"
    local baselined=0
    local b
    for b in "${PERSONAL_PATH_BASELINE[@]}"; do
      if [[ "${file}" == "${b}" ]]; then baselined=1; break; fi
    done
    if (( ! baselined )); then
      new_hits+="${line}"$'\n'
    fi
  done <<< "${hits}"

  if [[ -n "${new_hits}" ]]; then
    fail "personal-path: absolute personal home path in tracked files (not in the shrink-only baseline):"
    printf '%s' "${new_hits}"
    printf '  → use a relative path, an env var, or a config value. Do NOT add the file to the baseline in .github/scripts/repo_hygiene.sh — the baseline only shrinks.\n'
  else
    pass "personal-path: no new personal home paths (baseline: ${#PERSONAL_PATH_BASELINE[@]} historical files)"
  fi

  # A stale baseline entry is not a failure (the file's owner cleaned it up), but say so, so the
  # next edit to this script prunes it.
  for b in "${PERSONAL_PATH_BASELINE[@]}"; do
    if ! git grep -I -q -E "${pattern}" -- "${b}" 2>/dev/null; then
      printf '\033[33mNOTE\033[0m personal-path baseline entry no longer matches — prune it from repo_hygiene.sh: %s\n' "${b}"
    fi
  done
}

# ── 3. Dangling tracked symlinks ────────────────────────────────────────────────────────────────
# A tracked symlink must resolve, via a relative target, to a path INSIDE the repo that a fresh
# clone will actually contain (a tracked file, or a directory holding tracked files). Absolute
# targets and targets outside the repo dangle on every machine but this one; targets that exist
# locally but are gitignored dangle in every fresh clone — which is exactly how this bit us before.
check_symlinks() {
  local bad=""
  local mode _sha _stage path target resolved rel
  while read -r mode _sha _stage path; do
    [[ "${mode}" != "120000" ]] && continue
    target="$(readlink "${path}" || true)"
    if [[ -z "${target}" ]]; then
      bad+="  ${path} → (unreadable link)"$'\n'
      continue
    fi
    if [[ "${target}" = /* ]]; then
      bad+="  ${path} → ${target} (absolute target; breaks on any other machine)"$'\n'
      continue
    fi
    resolved="$(realpath -m -- "$(dirname -- "${path}")/${target}")"
    case "${resolved}/" in
      "${REPO_ROOT}/"*) ;;
      *)
        bad+="  ${path} → ${target} (escapes the repo; dangles in a fresh clone)"$'\n'
        continue
        ;;
    esac
    rel="${resolved#"${REPO_ROOT}"/}"
    # Present in a fresh clone iff the resolved path is a tracked file or a prefix directory of
    # one. git ls-files on the exact path covers both (a tracked dir means files under it match).
    if [[ -z "$(git ls-files -- "${rel}")" ]]; then
      bad+="  ${path} → ${target} (target is not tracked — gitignored or missing; dangles in a fresh clone)"$'\n'
    fi
  done < <(git ls-files -s)

  if [[ -n "${bad}" ]]; then
    fail "dangling-symlink: tracked symlink(s) that will not resolve in a fresh clone:"
    printf '%s' "${bad}"
    printf '  → point the link at a tracked, in-repo path, or replace the symlink with the real file / a config value.\n'
  else
    pass "dangling-symlink: all tracked symlinks resolve to tracked, in-repo targets"
  fi
}

# ── 4. Tracked .env files ───────────────────────────────────────────────────────────────────────
check_env_files() {
  local hits
  hits="$(git ls-files | grep -E '(^|/)\.env(\.|$)' | grep -v -E '(^|/)\.env\.example$' || true)"
  if [[ -n "${hits}" ]]; then
    fail "tracked-env: .env file(s) are tracked (only .env.example is allowed):"
    printf '%s\n' "${hits}" | sed 's/^/  /'
    printf '  → git rm --cached the file, rotate any secrets it held, and keep real env files gitignored.\n'
  else
    pass "tracked-env: no .env files tracked (only .env.example variants)"
  fi
}

check_account_number
check_personal_paths
check_symlinks
check_env_files

if (( FAILURES )); then
  printf '\n\033[31m✗ repo hygiene failed — fix the findings above before merging.\033[0m\n'
  exit 1
fi
printf '\n\033[32m✓ repo hygiene clean\033[0m\n'
