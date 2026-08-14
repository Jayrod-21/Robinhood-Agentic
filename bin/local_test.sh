#!/usr/bin/env bash
# local_test.sh — the authoritative local test gate.
#
# WHY THIS EXISTS
#   Running `.venv/bin/python -m pytest` on this machine proves the suite passes on the HOST's
#   Python 3.14. CI runs 3.12, and the containers ship 3.12. Those are different claims, and the
#   difference has already bitten this project's sibling (9b Korean Master's local-test.sh header
#   says the same thing about the same host).
#
#   So every suite here runs in a container pinned to CI's toolchain. The result does not depend on
#   whatever happens to be installed locally, and a green run here means CI will be green for the
#   same reasons — not by coincidence.
#
#   The container images MUST track the versions in .github/workflows/ci.yml. If you bump one,
#   bump both in the same commit, or this gate stops meaning anything.
#
# HARD GATES (a failure fails the run):
#   1. ruff        — backend/app src db
#   2. screen      — pytest tests
#   3. backend     — pytest backend/tests
#   4. database    — pytest db/tests (testcontainers spins its own postgres:16-alpine). Also
#                    installs the CLI's crypto deps: db/tests/test_manage_operator.py imports
#                    bin/manage_operator.py, which SystemExits at import without them — which
#                    kills pytest COLLECTION, not just that test.
#   5. frontend    — npm ci && npm run build
#   6. shellcheck  — bin/*.sh
#
# Every suite runs even if an earlier one fails, so one run shows every problem rather than
# revealing them one at a time.
#
# Usage:
#   bin/local_test.sh          # everything
#   bin/local_test.sh --fast   # skip the database and frontend suites. NOT a gate — inner loop only.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Keep in lockstep with .github/workflows/ci.yml.
readonly PY_IMAGE="python:3.12-slim"
readonly NODE_IMAGE="node:20-slim"
readonly RUFF_VERSION="0.16.0"

FAST=0
case "${1:-}" in
  --fast) FAST=1 ;;
  "") ;;
  *) echo "usage: $(basename "$0") [--fast]" >&2; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "✗ docker not found" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "✗ Docker daemon unreachable. On Linux: sudo systemctl start docker" >&2; exit 2; }

RESULTS=()
HARD_FAIL=0

record() { RESULTS+=("$1|$2|$3"); [[ "$1" == "FAIL" ]] && HARD_FAIL=1; return 0; }

# Runs a suite without letting a failure abort the script, so every suite reports.
hard() {
  local name="$1"; shift
  printf '\n\033[1m── %s ─────────────────────────────────────\033[0m\n' "${name}"
  if "$@"; then record PASS HARD "${name}"; else record FAIL HARD "${name}"; fi
}

# Non-root so nothing writes root-owned artefacts into the working tree. Consequence: pip falls back
# to a user install under $HOME, so console scripts land in /tmp/.local/bin rather than /usr/local/bin.
# Suites invoked as `python -m <mod>` are unaffected; anything calling a binary needs PY_BIN on PATH.
readonly PY_BIN="/tmp/.local/bin"
docker_py() { docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e "PATH=${PY_BIN}:/usr/local/bin:/usr/bin:/bin" "$@"; }

suite_ruff() {
  # `python -m ruff` rather than the console script: it works regardless of where a non-root pip
  # placed the binary. `--no-cache` because the repo is mounted read-only and ruff otherwise tries
  # to write .ruff_cache into it — and a gate should not be reusing a cache anyway.
  docker_py -v "${REPO_ROOT}:/repo:ro" -w /repo "${PY_IMAGE}" \
    sh -ec "pip install --quiet --no-cache-dir --disable-pip-version-check ruff==${RUFF_VERSION} && python -m ruff check --no-cache backend/app src db"
}

suite_screen() {
  docker_py -v "${REPO_ROOT}:/repo:ro" -w /repo "${PY_IMAGE}" \
    sh -ec 'pip install --quiet --no-cache-dir --disable-pip-version-check -r requirements.txt >/dev/null && python -m pytest tests -q -p no:cacheprovider'
}

suite_backend() {
  docker_py -v "${REPO_ROOT}:/repo:ro" -w /repo -e PYTHONPATH=/repo "${PY_IMAGE}" \
    sh -ec 'pip install --quiet --no-cache-dir --disable-pip-version-check -r backend/requirements.txt >/dev/null && python -m pytest backend/tests -q -p no:cacheprovider'
}

suite_database() {
  # testcontainers starts a SIBLING postgres through the host daemon, so it needs the socket and
  # host networking to reach that container's published port.
  #
  # Running non-root means the container user must be in the socket's group or every Docker call
  # fails with a permission error that surfaces as an opaque `docker.errors` on 38 tests. The gid is
  # read from the socket rather than hard-coded, since it differs across hosts.
  #
  # Mounting the socket is acceptable for a throwaway test container on the operator's own machine.
  # No application container ever receives it.
  local docker_gid
  docker_gid="$(stat -c %g /var/run/docker.sock)"
  docker_py --network host --group-add "${docker_gid}" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "${REPO_ROOT}:/repo:ro" -w /repo "${PY_IMAGE}" \
    sh -ec 'pip install --quiet --no-cache-dir --disable-pip-version-check "psycopg[binary]==3.3.4" "testcontainers[postgres]>=4,<5" "pytest>=8" "argon2-cffi==25.1.0" "pyotp==2.10.0" "cryptography==50.0.0" >/dev/null && python -m pytest db/tests -q -p no:cacheprovider'
}

suite_frontend() {
  # Source mounted READ-ONLY with node_modules and .next as writable anonymous volumes: deps install
  # fresh from the lockfile exactly as CI does, and nothing lands in the working tree.
  #
  # This one runs as ROOT, unlike the Python suites. An anonymous volume is created root-owned, so a
  # non-root user cannot populate it — npm fails with EACCES before installing a single package.
  # Root is safe here precisely because the only writable paths are the two throwaway volumes; the
  # source itself is read-only, so no root-owned file can appear in the repo.
  docker run --rm \
    -v "${REPO_ROOT}/frontend":/app:ro -v /app/node_modules -v /app/.next \
    -w /app -e NEXT_TELEMETRY_DISABLED=1 "${NODE_IMAGE}" \
    sh -ec 'npm ci --no-audit --no-fund && npm run build'
}

suite_shellcheck() {
  command -v shellcheck >/dev/null 2>&1 || { echo "shellcheck not installed on host — skipping"; return 0; }
  shellcheck -x bin/*.sh
}

hard "ruff"       suite_ruff
hard "screen"     suite_screen
hard "backend"    suite_backend
hard "shellcheck" suite_shellcheck
if (( FAST == 0 )); then
  hard "database" suite_database
  hard "frontend" suite_frontend
else
  printf '\n\033[33m--fast: database and frontend suites SKIPPED. This is not a gate.\033[0m\n'
fi

printf '\n\033[1m── summary ─────────────────────────────────\033[0m\n'
for line in "${RESULTS[@]}"; do
  IFS='|' read -r status sev name <<< "${line}"
  if [[ "${status}" == "PASS" ]]; then
    printf '  \033[32m%-6s\033[0m %-5s %s\n' "${status}" "${sev}" "${name}"
  else
    printf '  \033[31m%-6s\033[0m %-5s %s\n' "${status}" "${sev}" "${name}"
  fi
done

if (( HARD_FAIL )); then
  printf '\n\033[31m✗ a hard gate failed. Do NOT build or deploy until green.\033[0m\n'
  exit 1
fi
printf '\n\033[32m✓ all hard gates passed\033[0m\n'
