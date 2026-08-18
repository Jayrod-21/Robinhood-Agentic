#!/usr/bin/env bash
# stack_health.sh — is the whole stack actually up? Runs from cron every 5 minutes.
#
# WHY THIS EXISTS
#   On 2026-08-18 the rh-db container stopped existing. Not stopped — gone. Postgres later reported
#   "database system was not properly shut down", so it was killed rather than asked to stop. Every
#   database-backed page served 503 for five minutes and nothing noticed; it was found by accident.
#   The data survived only because the volume is declared external and no compose command can
#   remove it.
#
#   Nothing in this project watched the things it depends on. This does.
#
# EVERY CHECK REPORTS, PASS OR FAIL
#   The same rule the reconciliation guardrails follow: a check that only speaks when broken leaves
#   "was this even looked at?" unanswerable. All checks run — no early exit — so one failure never
#   hides the state of everything after it.
#
# WHAT IT DOES NOT DO BY DEFAULT
#   It does not repair. --repair recreates rh-db when the container is ABSENT (bin/db_up.sh is
#   idempotent and the volume is external, so that is safe), and even then it still exits non-zero
#   and still alerts — because a stack that quietly heals a recurring fault teaches you it never
#   happened. Enable it deliberately, in cron, once you have seen the failure at least once.
#
# EXIT CODES
#   0 everything passed · 1 at least one check failed

set -uo pipefail   # NOT -e: a failing check is data, not a reason to stop checking

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}" || exit 2

# The alerting path is itself a dependency, so it is verified rather than assumed. An earlier
# version sourced this unconditionally and, when the file was missing, ran every check, wrote the
# status file, and THEN died on "notify_transition: command not found" — losing the alert, which is
# the one output that reaches a human. A health check that cannot report is not a health check, so
# its own reporting path is a check.
NOTIFY_OK=true
if [[ -r "${SCRIPT_DIR}/lib_notify.sh" ]]; then
  # shellcheck source=bin/lib_notify.sh
  source "${SCRIPT_DIR}/lib_notify.sh"
  declare -F notify_transition >/dev/null || NOTIFY_OK=false
else
  NOTIFY_OK=false
fi
if ! ${NOTIFY_OK}; then
  notify_transition() { :; }   # so the run still completes and still writes the status file
fi

REPAIR=false
QUIET=false
for arg in "$@"; do
  case "${arg}" in
    --repair) REPAIR=true ;;
    --quiet)  QUIET=true ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: ${arg}" >&2; exit 2 ;;
  esac
done

DASH_PORT="${AGENTIC_DASH_PORT:-1855}"
SNAPSHOT_MAX_AGE_S="${AGENTIC_SNAPSHOT_MAX_AGE_S:-300}"   # alpaca_sync runs every minute
STATUS_JSON="${PROJECT_DIR}/data/stack_health.json"

PASSES=()
FAILURES=()

pass() { PASSES+=("$1"); ${QUIET} || printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { FAILURES+=("$1: $2"); ${QUIET} || printf '  \033[31mFAIL\033[0m %s — %s\n' "$1" "$2"; }

# ── containers ────────────────────────────────────────────────────────────────────────────────
# Checked by NAME rather than by compose project: the incident was a container that no longer
# existed, and `compose ps` on a stack whose container is gone reports an empty list, not a fault.

check_container() {
  local name="$1" want_health="$2"
  local state
  state="$(docker inspect --format '{{.State.Status}}' "${name}" 2>/dev/null)" || {
    fail "container:${name}" "does not exist"
    return 1
  }
  if [[ "${state}" != "running" ]]; then
    fail "container:${name}" "state=${state}"
    return 1
  fi
  if [[ "${want_health}" == "healthy" ]]; then
    local health
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${name}" 2>/dev/null)"
    if [[ "${health}" != "healthy" && "${health}" != "none" ]]; then
      fail "container:${name}" "health=${health}"
      return 1
    fi
  fi
  pass "container:${name}"
  return 0
}

check_container rh-db healthy || {
  if ${REPAIR} && ! docker inspect rh-db >/dev/null 2>&1; then
    echo "  → --repair: recreating rh-db (volume is external; data is not touched)"
    if bash "${SCRIPT_DIR}/db_up.sh" >/dev/null 2>&1; then
      fail "repair:rh-db" "container was MISSING and has been recreated — investigate why it vanished"
    else
      fail "repair:rh-db" "container was missing and db_up.sh could not recreate it"
    fi
  fi
}
check_container deploy-backend-1 healthy
check_container deploy-frontend-1 running
check_container deploy-caddy-1 running

# ── the database actually answers ─────────────────────────────────────────────────────────────
# A running container is not a working database. This is the check that would have caught the
# incident even if the container had merely been wedged rather than removed.
if docker exec rh-db pg_isready -q >/dev/null 2>&1; then
  pass "db:accepting-connections"
else
  fail "db:accepting-connections" "pg_isready says no"
fi

# ── the app answers, end to end ───────────────────────────────────────────────────────────────
# Through Caddy on the loopback port, which exercises the proxy and the backend together.
code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:${DASH_PORT}/api/health" 2>/dev/null)"
if [[ "${code}" == "200" ]]; then
  pass "http:api-health"
else
  fail "http:api-health" "GET /api/health returned ${code:-no response}"
fi

code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:${DASH_PORT}/" 2>/dev/null)"
if [[ "${code}" == "200" ]]; then
  pass "http:dashboard"
else
  fail "http:dashboard" "GET / returned ${code:-no response}"
fi

# ── the minute job is alive ───────────────────────────────────────────────────────────────────
# The fallback snapshot's mtime is a heartbeat for bin/alpaca_sync.sh. If it stops being written,
# the file silently reverts to being stale — the exact condition that made it useless before.
SNAPSHOT="${PROJECT_DIR}/data/account_snapshot.json"
if [[ -f "${SNAPSHOT}" ]]; then
  age=$(( $(date +%s) - $(stat -c %Y "${SNAPSHOT}") ))
  if (( age <= SNAPSHOT_MAX_AGE_S )); then
    pass "freshness:fallback-snapshot (${age}s)"
  else
    fail "freshness:fallback-snapshot" "${age}s old (>${SNAPSHOT_MAX_AGE_S}s) — is the alpaca_sync cron running?"
  fi
else
  fail "freshness:fallback-snapshot" "${SNAPSHOT} does not exist"
fi

# ── the schedule itself is installed ──────────────────────────────────────────────────────────
# A job that was never installed and a job that is failing look identical from the outside: no
# fresh output either way. This distinguishes them.
if ${NOTIFY_OK}; then
  pass "notify:available"
else
  fail "notify:available" "bin/lib_notify.sh is missing or unreadable — failures cannot reach anyone"
fi

crontab_text="$(crontab -l 2>/dev/null)"
for job in alpaca_sync.sh nightly_marks.sh; do
  if grep -q "${job}" <<<"${crontab_text}"; then
    pass "cron:${job}"
  else
    fail "cron:${job}" "not in crontab — install deploy/crontab.example"
  fi
done

# ── report ────────────────────────────────────────────────────────────────────────────────────
checked_at="$(date -u +%FT%TZ)"
if (( ${#FAILURES[@]} == 0 )); then
  status="ok"; detail="${#PASSES[@]} checks passed"
else
  status="fail"; detail="$(printf '%s; ' "${FAILURES[@]}")"; detail="${detail%; }"
fi

mkdir -p "${PROJECT_DIR}/data"
{
  printf '{\n  "checked_at": "%s",\n  "status": "%s",\n  "passed": %d,\n  "failed": %d,\n  "failures": [' \
    "${checked_at}" "${status}" "${#PASSES[@]}" "${#FAILURES[@]}"
  for i in "${!FAILURES[@]}"; do
    (( i > 0 )) && printf ', '
    printf '"%s"' "${FAILURES[$i]//\"/\\\"}"
  done
  printf ']\n}\n'
} >"${STATUS_JSON}"

notify_transition "stack-health" "${status}" "3b stack health" "${detail}"

${QUIET} || { echo; echo "${checked_at}  ${status}  (${#PASSES[@]} passed, ${#FAILURES[@]} failed)"; }
(( ${#FAILURES[@]} == 0 )) || exit 1
