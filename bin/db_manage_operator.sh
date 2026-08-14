#!/usr/bin/env bash
# db_manage_operator.sh — run bin/manage_operator.py (the operator-account CLI, AUTH_THREAT_MODEL
# §4) against the 3b database.
#
# The database is on `rh-internal` with no host port (ADR-001), so the CLI runs from a container
# attached to that network — the same pattern as bin/db_migrate.sh and bin/db_mark.sh. It joins
# ONLY rh-internal: account lifecycle needs no egress, and a container that cannot call out
# cannot leak what it handles. It connects as the privileged POSTGRES_USER role deliberately:
# migration 012 grants `rh_auth` no ability to create operators, disable them, or set passwords —
# the lifecycle surface is THIS wrapper on the host, not the app (§5.7).
#
# The image is rh-operator:local — rh-migrate:local plus argon2-cffi, pyotp, and cryptography,
# built here from an inline Dockerfile because the migration runner has no business carrying
# password-hashing libraries (the db_corporate_actions.sh precedent, but with no context dir:
# nothing from the repo is baked in, the repo stays a read-only mount). Dependencies are
# exact-AND-hash-pinned like db/requirements.txt (Bar §3.11): a matching version string from a
# compromised index is not the artifact you reviewed; a matching sha256 is. The install happens
# at BUILD time (default bridge, has egress); at RUN time the container is egress-free by design.
#
# Usage — every argument passes straight through to manage_operator.py:
#   bin/db_manage_operator.sh seed --email jared@example.com          # prompts for the password
#   bin/db_manage_operator.sh disable --email jared@example.com
#   bin/db_manage_operator.sh unlock --email jared@example.com
#   bin/db_manage_operator.sh reset-password --email jared@example.com
#   bin/db_manage_operator.sh reset-totp --email jared@example.com
#
# TOTP_SECRET_ENC_KEY (needed by seed / reset-totp) is read from backend/.env — or from the
# caller's environment, which wins if set — and passed into the container BY NAME, never on any
# command line. Generate one with: openssl rand -base64 32
#
# Exit codes are the CLI's: 0 ok · 1 validation · 2 SQL failure · 3 connection failure.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
BACKEND_ENV="${PROJECT_DIR}/backend/.env"
BASE_IMAGE="rh-migrate:local"
IMAGE="rh-operator:local"
NETWORK="rh-internal"
DB_HOST="rh-db"

die() { echo "✗ $*" >&2; exit 1; }

# read_env_value <key> <file> — strict parse. The env files are DATA: `source` would execute them
# as shell, turning a password containing `$(…)`, a backtick, or a space into command execution
# or a silently truncated credential. grep+cut cannot execute anything. (db_migrate.sh precedent.)
read_env_value() {
  local line
  line="$(grep -m1 -E "^$1=" "$2")" || return 1
  printf '%s' "${line#*=}"
}

[[ -f "${DB_ENV}" ]] || die "db/.env missing — run bin/db_up.sh first"
docker network inspect "${NETWORK}" >/dev/null 2>&1 || die "network ${NETWORK} missing — run bin/db_up.sh first"
[[ "$(docker inspect --format '{{.State.Status}}' "${DB_HOST}" 2>/dev/null || echo missing)" == "running" ]] \
  || die "container ${DB_HOST} is not running — run bin/db_up.sh first"
docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1 \
  || die "image ${BASE_IMAGE} missing — run bin/db_migrate.sh status once to build it"

POSTGRES_USER="$(read_env_value POSTGRES_USER "${DB_ENV}")" || die "POSTGRES_USER missing from db/.env"
POSTGRES_PASSWORD="$(read_env_value POSTGRES_PASSWORD "${DB_ENV}")" || die "POSTGRES_PASSWORD missing from db/.env"
POSTGRES_DB="$(read_env_value POSTGRES_DB "${DB_ENV}")" || die "POSTGRES_DB missing from db/.env"

# The encryption key: caller's environment wins (lets a restore/staging run supply its own);
# otherwise backend/.env. Only seed and reset-totp encrypt, so only they hard-require it — the
# check runs here so the failure names the fix instead of surfacing mid-command in the container.
SUBCOMMAND="${1:-}"
if [[ -z "${TOTP_SECRET_ENC_KEY:-}" && -f "${BACKEND_ENV}" ]]; then
  TOTP_SECRET_ENC_KEY="$(read_env_value TOTP_SECRET_ENC_KEY "${BACKEND_ENV}")" || true
fi
if [[ "${SUBCOMMAND}" == "seed" || "${SUBCOMMAND}" == "reset-totp" ]]; then
  [[ -n "${TOTP_SECRET_ENC_KEY:-}" ]] || die "TOTP_SECRET_ENC_KEY not set and not in backend/.env — \
generate one with: openssl rand -base64 32   (put it in backend/.env, mode 0600, and back it up \
in the password vault: losing it strands every enrolled TOTP secret — AUTH_THREAT_MODEL §7)"
fi
export TOTP_SECRET_ENC_KEY

# ── image build ───────────────────────────────────────────────────────────────────────────────
# The Dockerfile is inline and context-free (`docker build -`): the ONLY inputs are this text and
# the base image, both hashed into a label so a changed pin or a rebuilt rh-migrate triggers a
# rebuild — and nothing else can (db_migrate.sh precedent). Wheels are cp312/manylinux, matching
# the base image's Python; hashes regenerated the same way as db/requirements.txt documents.
DOCKERFILE=$(cat <<'EOF'
FROM rh-migrate:local
USER 0
RUN printf '%s\n' \
      'argon2-cffi==25.1.0 --hash=sha256:fdc8b074db390fccb6eb4a3604ae7231f219aa669a2652e0f20e16ba513d5741' \
      'argon2-cffi-bindings==21.2.0 --hash=sha256:b746dba803a79238e925d9046a63aa26bf86ab2a2fe74ce6b009a1c3f5c8f2ae' \
      'cffi==2.1.1 --hash=sha256:c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf' \
      'pycparser==3.0 --hash=sha256:b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992' \
      'pyotp==2.10.0 --hash=sha256:1df2f6a1bcc3bb0716172a5215ddc2f8c7c7fd26a13df9927d52e1746934836c' \
      'cryptography==50.0.0 --hash=sha256:06a32a980526a6ab9a4b9bf8f7385800791e2bb960903cb6b530e4817509a3b7' \
      > /tmp/operator-requirements.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/operator-requirements.txt \
 && rm /tmp/operator-requirements.txt
USER 1001
WORKDIR /repo
ENTRYPOINT ["python", "/repo/bin/manage_operator.py"]
EOF
)

base_id="$(docker image inspect --format '{{.Id}}' "${BASE_IMAGE}")"
inputs_sha="$(printf '%s\n%s' "${DOCKERFILE}" "${base_id}" | sha256sum | cut -d' ' -f1)"
current_sha="$(docker image inspect --format '{{index .Config.Labels "rh.build_inputs_sha256"}}' "${IMAGE}" 2>/dev/null || true)"
if [[ "${current_sha}" != "${inputs_sha}" ]]; then
  echo "building ${IMAGE}…" >&2
  printf '%s' "${DOCKERFILE}" | docker build -q \
    --label "rh.build_inputs_sha256=${inputs_sha}" \
    -t "${IMAGE}" - >/dev/null
fi

# ── run ───────────────────────────────────────────────────────────────────────────────────────
# libpq-style PG* variables, passed by NAME (--env with no value): no credential ever appears in
# any argv and no URL is assembled — a password containing '@', '/', or '%' would corrupt a
# concatenated postgresql:// URL, and libpq reads these directly (db_migrate.sh precedent).
export PGHOST="${DB_HOST}"
export PGPORT=5432
export PGUSER="${POSTGRES_USER}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
export PGDATABASE="${POSTGRES_DB}"

docker_flags=(
  --rm
  --network "${NETWORK}"
  --env PGHOST --env PGPORT --env PGUSER --env PGPASSWORD --env PGDATABASE
  --env TOTP_SECRET_ENC_KEY
  --volume "${PROJECT_DIR}:/repo:ro"
  --init
  # Always interactive: seed/reset-password read the password from stdin (prompt or
  # --password-stdin), never from argv.
  --interactive
)
[[ -t 0 ]] && docker_flags+=(--tty)

exec docker run "${docker_flags[@]}" "${IMAGE}" "$@"
