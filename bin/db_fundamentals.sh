#!/usr/bin/env bash
# db_fundamentals.sh — run db/load_fundamentals.py (the FMP fundamentals ingest) against rh-db.
#
# The database is on `rh-internal` with no host port (ADR-001), so this runs from a container
# attached to that network — the same pattern as bin/db_migrate.sh and bin/db_manage_operator.sh.
#
# UNLIKE the other db wrappers, this container NEEDS EGRESS: the whole job is fetching from
# financialmodelingprep.com. It therefore joins the default bridge as well as rh-internal. That is
# a deliberate, narrow exception to the egress-free convention, and it is why the FMP key is passed
# by NAME rather than baked in, and why nothing here writes to any table but
# fundamentals_snapshots and data_sources.
#
# The image is rh-fundamentals:local — rh-migrate:local plus `requests` (the FMP transport), built
# from an inline Dockerfile with no build context. Dependencies are exact-AND-hash-pinned (Bar
# §3.11): a matching version string from a compromised index is not the artifact you reviewed; a
# matching sha256 is.
#
# Usage — arguments pass straight through to load_fundamentals.py:
#   bin/db_fundamentals.sh report
#   bin/db_fundamentals.sh load --symbols AAPL,MSFT,NVDA --dry-run
#   bin/db_fundamentals.sh load --symbols AAPL,MSFT,NVDA
#
# FMP_API_KEY is read from backend/.env (or the caller's environment, which wins) and passed into
# the container BY NAME — never on a command line, where `ps` would show it.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

DB_ENV="${PROJECT_DIR}/db/.env"
BACKEND_ENV="${PROJECT_DIR}/backend/.env"
BASE_IMAGE="rh-migrate:local"
IMAGE="rh-fundamentals:local"
NETWORK="rh-internal"
DB_HOST="rh-db"

die() { echo "✗ $*" >&2; exit 1; }

# The env files are DATA: `source` would execute them, turning a password containing `$(…)` into
# command execution. grep+cut cannot execute anything. (db_migrate.sh precedent.)
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

# The key: caller's environment wins; otherwise backend/.env. Checked HERE so the failure names the
# fix instead of surfacing as a 401 from inside the container halfway through a run.
if [[ -z "${FMP_API_KEY:-}" && -f "${BACKEND_ENV}" ]]; then
  FMP_API_KEY="$(read_env_value FMP_API_KEY "${BACKEND_ENV}")" || true
fi
SUBCOMMAND="${1:-}"
if [[ "${SUBCOMMAND}" == "load" ]]; then
  [[ -n "${FMP_API_KEY:-}" ]] || die "FMP_API_KEY not set and not in backend/.env — it is a paid \
credential; put it in backend/.env (mode 0600)"
fi
export FMP_API_KEY="${FMP_API_KEY:-}"
# Optional knobs, forwarded by name when set. Unset is a valid state for both (see src/fmp.py:
# no daily cap, and the per-minute default sits under the plan's ceiling).
export FMP_DAILY_CALL_BUDGET="${FMP_DAILY_CALL_BUDGET:-}"
export FMP_CALLS_PER_MINUTE="${FMP_CALLS_PER_MINUTE:-}"

# ── image build ───────────────────────────────────────────────────────────────────────────────
# Inline and context-free (`docker build -`): the ONLY inputs are this text and the base image,
# both hashed into a label so a changed pin or a rebuilt rh-migrate triggers a rebuild — and
# nothing else can. Wheels are py3-none-any except charset-normalizer, which ships binaries;
# that one is the cp312/manylinux x86_64 wheel matching the base image's Python.
DOCKERFILE=$(cat <<'EOF'
FROM rh-migrate:local
USER 0
RUN printf '%s\n' \
      'requests==2.34.2 --hash=sha256:2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0' \
      'urllib3==2.7.0 --hash=sha256:9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897' \
      'certifi==2026.7.22 --hash=sha256:62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775' \
      'charset-normalizer==3.4.9 --hash=sha256:68e5f26a1ad57ded6d1cfb85331d1c1a195314756471d97758c48498bb4dcdf5' \
      'idna==3.18 --hash=sha256:7f952cbe720b688055e3f87de14f5c3e5fdaa8bc3928985c4077ca689de849a2' \
      > /tmp/fundamentals-requirements.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/fundamentals-requirements.txt \
 && rm /tmp/fundamentals-requirements.txt
USER 1001
WORKDIR /repo
ENTRYPOINT ["python", "/repo/db/load_fundamentals.py"]
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
# libpq-style PG* variables passed by NAME: no credential appears in any argv, and no URL is
# assembled — a password containing '@', '/', or '%' would corrupt a concatenated postgresql:// URL
# (db_migrate.sh precedent). load_fundamentals.py connects with an empty DSN, which libpq reads
# from exactly these.
export PGHOST="${DB_HOST}"
export PGPORT=5432
export PGUSER="${POSTGRES_USER}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
export PGDATABASE="${POSTGRES_DB}"

docker_flags=(
  --rm
  --network "${NETWORK}"
  --env PGHOST --env PGPORT --env PGUSER --env PGPASSWORD --env PGDATABASE
  --env FMP_API_KEY --env FMP_DAILY_CALL_BUDGET --env FMP_CALLS_PER_MINUTE
  --volume "${PROJECT_DIR}:/repo:ro"
  --init
)
[[ -t 0 ]] && docker_flags+=(--tty)

# rh-internal is `internal: true` — no egress. This job MUST reach FMP, so a second network is
# attached after creation (a container can only be given one --network at `docker run` time).
# Done as create+connect+start rather than `--network bridge` so the DB-side attachment is never
# in question if the egress attach fails.
cid="$(docker create "${docker_flags[@]}" "${IMAGE}" "$@")"
trap 'docker rm -f "${cid}" >/dev/null 2>&1 || true' EXIT
docker network connect bridge "${cid}"
docker start -a "${cid}"
