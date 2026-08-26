#!/usr/bin/env bash
# db_collect_intraday.sh — one 30-minute intraday observation sweep (issue #133).
#
# Records price, market cap, volume and the price-derived ratios for every security in scope
# (held / debated / proposed), with a lineage FK to the statement row the denominators came from
# and the formula version that computed them. See db/collect_intraday.py.
#
#   bin/db_collect_intraday.sh --dry-run    # resolve scope, report it, write nothing
#   bin/db_collect_intraday.sh              # one sweep
#
# Outside the session window it records a 'skipped' run with the reason and writes no observation —
# a price that has not moved since Friday is not an observation, it is the same one again.
#
# DELEGATES to db_corporate_actions.sh, which already assembles DATABASE_URL from db/.env, passes
# it through the environment rather than argv, and attaches both the internal database network and
# the egress network. A second copy of that is a second place for a credential to reach `ps`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${FMP_API_KEY:-}" ]]; then
  ENV_FILE="$ROOT/backend/.env"
  [[ -f "$ENV_FILE" ]] || { echo "✗ FMP_API_KEY not exported and $ENV_FILE is missing" >&2; exit 3; }
  FMP_API_KEY="$(grep -m1 -E '^FMP_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
  export FMP_API_KEY
fi
[[ -n "${FMP_API_KEY}" ]] || { echo "✗ FMP_API_KEY is empty" >&2; exit 3; }

LOADER_SCRIPT="/repo/db/collect_intraday.py" exec "$ROOT/bin/db_corporate_actions.sh" "$@"
