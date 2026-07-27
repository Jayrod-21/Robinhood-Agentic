#!/usr/bin/env bash
# up.sh — bring the dashboard up on freshly-picked random ports.
#
# Steps:
#   1. Verify the Docker daemon is reachable (clear message if Docker Desktop is off).
#   2. Pick two fresh, verified-free random ports (bin/pick_ports.sh).
#   3. Export ports + backend/.env so docker compose can substitute them.
#   4. Build + start the backend and frontend containers.
#   5. Wait for the backend healthcheck, then start the host-side refresh daemon.
#   6. Print the dashboard URL.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

# Resolve a working docker CLI (under WSL the symlink can be stale; fall back to docker.exe).
DOCKER="docker"
if ! ${DOCKER} info >/dev/null 2>&1; then
  if command -v docker.exe >/dev/null 2>&1 && docker.exe info >/dev/null 2>&1; then
    DOCKER="docker.exe"
  else
    echo "✗ Docker daemon is not reachable." >&2
    echo >&2
    # The fix differs by host, and printing the WSL instructions on a native Linux box sends you
    # looking for a Docker Desktop that isn't there. Detect and say the right thing.
    if grep -qi microsoft /proc/version 2>/dev/null; then
      cat >&2 <<'MSG'
Start Docker Desktop on Windows, then enable WSL integration:
  Docker Desktop → Settings → Resources → WSL Integration → enable for this distro → Apply & Restart.
MSG
    else
      cat >&2 <<'MSG'
On Linux, start the engine and make sure your user can reach the socket:
  sudo systemctl start docker
  sudo usermod -aG docker "$USER"   # then log out and back in (or: newgrp docker)
MSG
    fi
    echo >&2
    echo "Re-run bin/up.sh once \`docker info\` succeeds." >&2
    exit 1
  fi
fi
echo "✓ Docker daemon reachable (${DOCKER})"

# Fresh random ports.
bash "${SCRIPT_DIR}/pick_ports.sh"
set -a
# shellcheck disable=SC1091
source "${PROJECT_DIR}/.env.ports"
[ -f "${PROJECT_DIR}/backend/.env" ] && source "${PROJECT_DIR}/backend/.env"
set +a

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "⚠ ANTHROPIC_API_KEY not set — account & scan work, but live debates/pipeline will 503."
fi

# The backend writes the refresh trigger, debate records, and the event store into these bind
# mounts. It runs as the HOST user (see docker-compose.yml `user:`) rather than the image's uid
# 1001, so these directories can stay private.
#
# They must be private: data/account_snapshot.json is the account's full position list and cost
# basis, and data/refresh.request is acted on by the host daemon purely because it EXISTS — a
# world-writable data/ lets any local account both read the holdings and forge a refresh, bypassing
# the API's cooldown entirely. The previous `chmod -R a+rwX` did exactly that.
HOST_UID="$(id -u)"; HOST_GID="$(id -g)"
export HOST_UID HOST_GID
mkdir -p "${PROJECT_DIR}/data" "${PROJECT_DIR}/logs/debates" "${PROJECT_DIR}/logs/refresh"
chmod 700 "${PROJECT_DIR}/data" "${PROJECT_DIR}/logs" "${PROJECT_DIR}/logs/debates" "${PROJECT_DIR}/logs/refresh"

echo "Building + starting containers (backend :${BACKEND_PORT}, frontend :${FRONTEND_PORT})…"
${DOCKER} compose up --build -d

# Wait for backend health. Branch on the outcome — never print a dashboard URL for a dead stack.
echo -n "Waiting for backend health"
healthy=0
for _ in $(seq 1 40); do
  if curl -fsS "http://localhost:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then
    healthy=1
    echo " ✓"
    break
  fi
  echo -n "."
  sleep 2
done

if [ "${healthy}" -ne 1 ]; then
  echo " ✗"
  echo "✗ Backend did not become healthy within 80s." >&2
  echo "  Check logs:  ${DOCKER} compose logs backend" >&2
  echo "  The refresh daemon was NOT started and no dashboard URL is printed." >&2
  exit 1
fi

# Start the host-side refresh daemon (the Refresh button needs it). Idempotent-ish: kill any prior.
pkill -f "bin/refresh_daemon.sh" 2>/dev/null || true
mkdir -p "${PROJECT_DIR}/logs/refresh"
nohup bash "${SCRIPT_DIR}/refresh_daemon.sh" >"${PROJECT_DIR}/logs/refresh/daemon.out" 2>&1 &
disown 2>/dev/null || true
echo "✓ Refresh daemon started (host-side)"

echo
echo "Dashboard:  http://localhost:${FRONTEND_PORT}"
echo "Backend API: http://localhost:${BACKEND_PORT}/api/health"
echo
echo "Stop with:  ${DOCKER} compose down   (and: pkill -f bin/refresh_daemon.sh)"
