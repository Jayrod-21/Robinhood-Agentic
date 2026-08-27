#!/usr/bin/env bash
# nightly_marks.sh — what cron runs after the close to grow the equity curve.
#
# WHY THIS EXISTS
#   portfolio_returns_daily had exactly ONE row: nothing was scheduled, so the curve never grew and
#   the performance page could not compute a Sharpe or a Sortino from a single point. Marking is
#   not something to do by hand — a missed day is a permanent hole, because a live mark is refused
#   once it is more than LIVE_MAX_AGE_DAYS old (mark_portfolios.py) and must then be backfilled as
#   what it is: a historical mark.
#
# THE THREE STEPS ARE ORDERED, AND THE ORDER MATTERS
#   1. SYNC   the broker's holdings into the kind='real' portfolio. A position opened today is
#             invisible to the valuation until this runs — the mirror is what the marking job reads,
#             not the broker. Skipping it marks yesterday's book at today's prices.
#   2. BARS   load the day's closes for held names. The marking job fetches nothing; it values what
#             is already stored, so a missing bar is a coverage hole rather than a slow mark.
#   3. SCORE  grade any debate judgment whose 5-session window has elapsed. Needs history, not
#             today's bars, so it runs before the mark and independently of it.
#   4. MARK   value the book for the latest session.
#
#   Each step must succeed before the next runs. Marking against a stale mirror or missing bars
#   produces a NUMBER rather than an error, and a wrong equity curve is worse than a short one —
#   it is the input to every performance statistic on the site.
#
# RUN IT AFTER THE CLOSE
#   The daily bar for an unfinished session is a partial print. Cron fires at 15:15 America/Denver
#   (17:15 ET), which is after the 16:00 ET close with room for the vendor to publish.
#
# EXIT CODES
#   0 ok (or a clean skip on a non-trading day) · 1 a step failed — read the log.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}" || exit 1

# shellcheck source=bin/lib_notify.sh
source "${SCRIPT_DIR}/lib_notify.sh"

LOG_DIR="${PROJECT_DIR}/logs/cron"
mkdir -p "${LOG_DIR}"
exec >>"${LOG_DIR}/$(date -u +%Y%m%d)-marks.log" 2>&1

echo "=== nightly_marks @ $(date -u +%FT%TZ) ==="

# Alpaca and FMP credentials live here; the DB credentials come from db/.env inside each wrapper.
#
# PARSED, NEVER SOURCED. bin/db_migrate.sh has said why since it was written — "the file is DATA:
# `source` would execute it as shell, turning a password containing `$(…)`, a backtick, or a space
# into command execution or a silently truncated credential" — and this script sourced it anyway.
#
# It broke on 2026-08-26, the day owner labels were added for the LLM cost split:
#
#     ANTHROPIC_API_KEY_NAME=Jared Anthropic
#     backend/.env: line 7: Jared: command not found
#
# Bash read that as "set NAME=Jared, then run the command `Anthropic`", `set -e` killed the script,
# and the equity curve and the daily bar load both stopped — silently, for two days, because a
# 144-byte log file looks like any other log file. Nothing in the value was malicious; a space was
# enough.
BACKEND_ENV="${PROJECT_DIR}/backend/.env"
[[ -f "${BACKEND_ENV}" ]] || { echo "✗ ${BACKEND_ENV} missing — no broker or vendor credentials"; exit 1; }

# KEY=VALUE only, comments and blanks skipped, value taken verbatim to the end of the line. No
# expansion, no word splitting, no command substitution — the shell never evaluates the value.
while IFS= read -r line; do
  [[ "${line}" =~ ^[[:space:]]*# ]] && continue
  [[ "${line}" =~ ^[[:space:]]*$ ]] && continue
  [[ "${line}" == *=* ]] || continue
  key="${line%%=*}"
  value="${line#*=}"
  [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  export "${key}=${value}"
done < "${BACKEND_ENV}"

step() {
  local name="$1"; shift
  echo "--- ${name}"
  # rc is captured from the STEP, immediately. An earlier version ran `if "$@"; then … fi` and read
  # $? in the else branch, where it was the status of the preceding `echo` — always 0. So a failed
  # step logged "exit 0" and, because the function returned 0, the next step ran anyway: the mark
  # went ahead against an unsynced mirror. The whole point of ordering these steps is that this
  # cannot happen.
  local rc=0
  "$@" || rc=$?
  if (( rc == 0 )); then
    echo "--- ${name}: ok"
    return 0
  fi
  echo "✗ ${name} failed (exit ${rc}) — later steps skipped so nothing marks against stale inputs"
  notify_transition "nightly-marks" "fail" "3b daily marking" \
    "${name} failed (exit ${rc}) — the equity curve did not advance today"
  exit 1
}

step "sync broker holdings" env LOADER_SCRIPT=/repo/db/sync_real_portfolio.py \
  bash "${SCRIPT_DIR}/db_corporate_actions.sh"

step "load daily bars" env LOADER_SCRIPT=/repo/db/load_daily_bars_fmp.py \
  bash "${SCRIPT_DIR}/db_corporate_actions.sh"

# 3. Score judgments whose horizon has elapsed.
#
# Before the mark, and outside the all-or-nothing chain, on purpose. It grades calls made at least
# five sessions ago, so it needs history rather than today's bars — which means it can succeed on
# exactly the days the mark cannot, such as a run before the close. Chaining it behind the mark
# would have thrown away a day of calibration data every time a bar was late.
#
# A failure here costs calibration coverage, never the equity curve, so it does not stop the run.
if out="$(LOADER_SCRIPT=/repo/db/score_judgments.py bash "${SCRIPT_DIR}/db_corporate_actions.sh" 2>&1)"; then
  echo "--- score judgments: ok"
  echo "${out}" | tail -2
else
  echo "✗ scoring failed — the equity curve is unaffected; calibration will catch up tomorrow"
  echo "${out}" | tail -5
fi

# 4. Mark the book. Last, because it is the step with the strictest inputs.
# The mark job consults market_calendar and refuses on a non-trading day. That refusal is a normal
# outcome for a cron that fires every weekday — holidays are not failures — so it is reported and
# swallowed rather than paged on. Any OTHER validation failure is a real one and propagates.
set +e
out="$(bash "${SCRIPT_DIR}/db_mark.sh" live 2>&1)"
rc=$?
set -e
echo "${out}"
if [[ ${rc} -ne 0 ]]; then
  if grep -q "is not a trading session\|no trading session on or before" <<<"${out}"; then
    echo "--- mark: skipped, not a trading session"
    exit 0
  fi
  echo "✗ mark failed (exit ${rc})"
  notify_transition "nightly-marks" "fail" "3b daily marking" \
    "the mark failed (exit ${rc}) — the equity curve did not advance today"
  exit 1
fi
notify_transition "nightly-marks" "ok" "3b daily marking" "the book was marked"
echo "=== done @ $(date -u +%FT%TZ)"
