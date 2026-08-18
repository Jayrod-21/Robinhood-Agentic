#!/usr/bin/env bash
# lib_notify.sh — where a background job's bad news actually goes on this machine. Source, don't run.
#
# THE PROBLEM THIS SOLVES
#   Every cron comment in this repo used to say "cron mails only failures". That is false here:
#   M has no MTA installed and /var/mail is empty, so cron discards a job's output entirely. Three
#   scheduled jobs were reporting failures into nothing.
#
#   The database container disappearing is the case that proved it matters. Nothing noticed for five
#   minutes, and only because someone happened to be looking.
#
# THREE DESTINATIONS, DELIBERATELY
#   1. A LOG FILE — the durable record, greppable after the fact. Always written.
#   2. A STATE FILE — what was wrong last time, so repeats stay quiet (see below).
#   3. A DESKTOP NOTIFICATION — the only channel on this box that interrupts a human. Best-effort:
#      cron has no DBUS_SESSION_BUS_ADDRESS, so it is pointed at the user's session bus explicitly.
#      If that fails, the log still has it and the caller is never failed by a notifier.
#
# WHY IT DE-DUPLICATES
#   A five-minute check against an hour-long outage is twelve identical popups, and twelve identical
#   popups train someone to dismiss the thirteenth. So a notification fires only when the situation
#   CHANGES: a new failure, a different set of failures, or a recovery. Recovery is announced too —
#   "it is fixed" is information, and without it the last thing on screen is a stale alarm.

_notify_state_dir() {
  local dir="${AGENTIC_NOTIFY_STATE_DIR:-${HOME}/.local/state/agentic}"
  mkdir -p "${dir}" 2>/dev/null || true
  printf '%s' "${dir}"
}

# notify_desktop <urgency> <title> <body> — best-effort; never returns non-zero to the caller.
notify_desktop() {
  local urgency="$1" title="$2" body="$3"
  command -v notify-send >/dev/null 2>&1 || return 0
  local bus="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
  DBUS_SESSION_BUS_ADDRESS="${bus}" notify-send --urgency="${urgency}" \
    --app-name="3b Agentic" "${title}" "${body}" >/dev/null 2>&1 || true
}

# notify_transition <key> <status> <title> <detail>
#   status: ok | fail
#   Fires a desktop notification only when <status>+<detail> differs from the last call for <key>.
#   Always appends to the log. Returns 0 always — reporting must not break the job reporting.
notify_transition() {
  local key="$1" status="$2" title="$3" detail="$4"
  local dir; dir="$(_notify_state_dir)"
  local state="${dir}/${key}.state"
  local log="${dir}/${key}.log"
  local now; now="$(date -u +%FT%TZ)"
  local fingerprint="${status}|${detail}"
  local previous=""
  [[ -f "${state}" ]] && previous="$(cat "${state}" 2>/dev/null || true)"

  printf '%s %s %s\n' "${now}" "${status}" "${detail}" >>"${log}" 2>/dev/null || true

  if [[ "${fingerprint}" == "${previous}" ]]; then
    return 0   # same situation as last run — logged, not re-announced
  fi
  printf '%s' "${fingerprint}" >"${state}" 2>/dev/null || true

  if [[ "${status}" == "fail" ]]; then
    notify_desktop critical "${title}" "${detail}"
  elif [[ -n "${previous}" && "${previous}" != ok\|* ]]; then
    # Only announce a recovery if it followed a failure we announced. A first-ever OK is not news.
    notify_desktop normal "${title} — recovered" "${detail}"
  fi
  return 0
}
