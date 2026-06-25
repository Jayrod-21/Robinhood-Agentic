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

# Resolve a working docker CLI (WSL symlink can be stale; fall back to docker.exe).
DOCKER="docker"
if ! ${DOCKER} info >/dev/null 2>&1; then
  if command -v docker.exe >/dev/null 2>&1 && docker.exe info >/dev/null 2>&1; then
    DOCKER="docker.exe"
  else
    cat >&2 <<'MSG'
✗ Docker daemon is not reachable.

Start Docker Desktop on Windows, then enable WSL integration:
  Docker Desktop → Settings → Resources → WSL Integration → enable for this distro → Apply & Restart.

Re-run bin/up.sh once `docker info` succeeds.
MSG
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

# The backend runs non-root (uid 1001); make the bind-mounted dirs writable so it can persist the
# refresh trigger, debate records, and the event store. a+rwX is robust to re-creation/ownership flips.
mkdir -p "${PROJECT_DIR}/data" "${PROJECT_DIR}/logs/debates" "${PROJECT_DIR}/logs/refresh"
chmod -R a+rwX "${PROJECT_DIR}/data" "${PROJECT_DIR}/logs" 2>/dev/null || true

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
