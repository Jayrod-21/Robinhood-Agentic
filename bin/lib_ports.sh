#!/usr/bin/env bash
# lib_ports.sh — shared free-port verification. Sourced, never executed.
#
# A port is treated as free ONLY if it passes three independent checks, because each one alone has
# a blind spot:
#
#   1. socket bind   — Python binds 0.0.0.0:<port> with SO_REUSEADDR OFF. Catches "something holds
#                      this right now" for IPv4, but says nothing about intent. (IPv6-only
#                      listeners and UDP are invisible to it — check #2 covers IPv6 TCP, which is
#                      why the verdict is the AND of all three, not any one "authoritative" check.)
#   2. ss listeners  — the kernel's TCP listening set, IPv4 and IPv6; catches a listener the bind
#                      test could race with.
#   3. docker ports  — every host-port binding declared by any container, RUNNING OR STOPPED, read
#                      from HostConfig.PortBindings (which survives a stop, unlike the `docker ps`
#                      Ports column, which renders empty for stopped containers). A stopped
#                      container still owns its mapping in compose terms — 9b's km-lb briefly stops
#                      during a blue/green flip, and taking 1840/1841 in that window would break the
#                      stack on its next start. Published RANGES (e.g. km-lb's 1840-1841) are
#                      expanded port-by-port.
#
# Standing rule (2026-07-27): when creating any container that binds a host port, verify the
# port is free first. If it is taken, pick a DIFFERENT port — never stop or reuse whatever holds it.
# M runs several live stacks side by side (9b Korean Master owns 1840-1843 plus its DB), so "the
# port I always use" is not a safe assumption.
#
# Range is deliberately below the conventional Linux ephemeral floor (32768) so a chosen port
# cannot collide with a kernel-allocated outbound socket.
#
# Residual race: a TOCTOU window remains between the check and the bind. Callers that care should
# re-verify immediately before committing (see pick_ports.sh).

# Guard against direct execution — this file only defines functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "lib_ports.sh is a library; source it, don't run it." >&2
  exit 2
fi

PORT_MIN="${PORT_MIN:-20000}"
PORT_MAX="${PORT_MAX:-32767}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-15}"

# Host ports bound by ANY container, running or stopped (best-effort; empty if the daemon is down).
# Reads HostConfig.PortBindings — the authoritative store that persists across container stop —
# not `docker ps --format {{.Ports}}`, which (a) omits stopped containers entirely and (b) renders
# ranges like `0.0.0.0:1840-1841->1840-1841/tcp` that a `:PORT->` regex cannot parse. Both failure
# modes produced false "free" verdicts on 9b's live ports before this was fixed.
# A HostPort entry may itself be a range ("1840-1841"); the awk expands it to individual ports.
docker_published_ports() {
  command -v docker >/dev/null 2>&1 || return 0
  # $bindings below is Go template syntax, not a shell variable; single quotes keep the shell's
  # hands off it.
  # shellcheck disable=SC2016
  docker ps -aq 2>/dev/null \
    | xargs -r docker inspect --format \
        '{{range $p, $bindings := .HostConfig.PortBindings}}{{range $bindings}}{{println .HostPort}}{{end}}{{end}}' \
        2>/dev/null \
    | grep -oE '^[0-9]+(-[0-9]+)?$' \
    | awk -F- 'NF==1 {print $1} NF==2 {for (i=$1; i<=$2; i++) print i}' \
    | sort -un || true
}

# Ports in the kernel's TCP listening set.
ss_listening_ports() {
  ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$' || true
}

# Refresh both snapshots into the caller's SS_PORTS / DOCKER_PORTS. Call before a pick, and again
# immediately before committing a choice.
refresh_port_snapshots() {
  SS_PORTS="$(ss_listening_ports)"
  DOCKER_PORTS="$(docker_published_ports)"
}

# port_is_free <port> — all three checks. Expects SS_PORTS / DOCKER_PORTS to be populated.
port_is_free() {
  local port="$1"

  if printf '%s\n' "${SS_PORTS:-}" | grep -qx "${port}"; then
    return 1
  fi
  if printf '%s\n' "${DOCKER_PORTS:-}" | grep -qx "${port}"; then
    return 1
  fi
  python3 - "${port}" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

# pick_free_port [exclude...] — one free random port, avoiding the listed exclusions.
# Uses secrets.randbelow so each attempt is a fresh cryptographically-strong draw.
pick_free_port() {
  local exclude="$*"
  local port
  # `_` because the counter is only an attempt budget — each draw is independent and random.
  for _ in $(seq 1 "${MAX_ATTEMPTS}"); do
    # Bounds travel as argv, not interpolated into the program text (§3.5: never build code from
    # variables), and are validated as integers rather than trusted.
    port="$(python3 - "${PORT_MIN}" "${PORT_MAX}" <<'PY'
import secrets, sys
lo, hi = int(sys.argv[1]), int(sys.argv[2])
if hi < lo:
    sys.exit(f"pick_free_port: PORT_MAX ({hi}) < PORT_MIN ({lo})")
print(lo + secrets.randbelow(hi - lo + 1))
PY
)" || return 1
    case " ${exclude} " in *" ${port} "*) continue ;; esac
    if port_is_free "${port}"; then
      printf '%s' "${port}"
      return 0
    fi
  done
  return 1
}
