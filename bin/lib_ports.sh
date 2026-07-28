#!/usr/bin/env bash
# lib_ports.sh — shared free-port verification. Sourced, never executed.
#
# A port is treated as free ONLY if it passes three independent checks, because each one alone has
# a blind spot:
#
#   1. socket bind   — Python binds 0.0.0.0:<port> with SO_REUSEADDR OFF. Authoritative for
#                      "something holds this right now", but says nothing about intent.
#   2. ss listeners  — catches a listener the bind test could race with.
#   3. docker ports  — catches a port published by a running container. A container that is
#                      *stopped* still owns its port mapping in compose terms, and `ss` won't
#                      show it; taking that port would break the stack on its next start.
#
# Standing rule (Jared, 2026-07-27): when creating any container that binds a host port, verify the
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

# Ports published by any running container (best-effort; empty if the daemon is down).
docker_published_ports() {
  command -v docker >/dev/null 2>&1 || return 0
  docker ps --format '{{.Ports}}' 2>/dev/null \
    | grep -oE ':[0-9]+->' | tr -dc '0-9\n' || true
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

# pick_free_port [exclude...] — one free random port, avoiding the space-separated exclusions.
# Uses secrets.randbelow so each attempt is a fresh cryptographically-strong draw.
pick_free_port() {
  local exclude="${1:-}"
  local port
  # `_` because the counter is only an attempt budget — each draw is independent and random.
  for _ in $(seq 1 "${MAX_ATTEMPTS}"); do
    port="$(python3 -c "import secrets; print(${PORT_MIN} + secrets.randbelow(${PORT_MAX}-${PORT_MIN}+1))")"
    case " ${exclude} " in *" ${port} "*) continue ;; esac
    if port_is_free "${port}"; then
      printf '%s' "${port}"
      return 0
    fi
  done
  return 1
}
