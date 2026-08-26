#!/usr/bin/env bash
# db_instrument_types.sh — classify every security, and close the gap holes instrument form explains.
#
# Issue #41. Two bulk FMP requests populate securities.security_type and .name for all ~19.7k rows,
# then gap holes on warrants/units/rights are dispositioned terminal. See db/load_instrument_types.py
# for what it will and will not write (it will NOT write exchange or sector — the bulk lists do not
# carry them, and a guessed exchange is worse than the NULL that is there now).
#
#   bin/db_instrument_types.sh both --dry-run     # report, write nothing
#   bin/db_instrument_types.sh both               # classify, then disposition
#   bin/db_instrument_types.sh classify
#   bin/db_instrument_types.sh disposition
#
# DELEGATES to db_corporate_actions.sh rather than re-implementing the runner. That script already
# solves the two things this needs and both are easy to get wrong: it assembles DATABASE_URL from
# db/.env and passes it through the ENVIRONMENT so the password never reaches argv, and it attaches
# BOTH the internal database network and the egress network — a loader that needs the provider AND
# the database. It also forwards FMP_API_KEY only when set. A second copy of that logic is a second
# place for a credential to leak into `ps`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The provider lists are the whole point of this loader, so unlike the delegate — where FMP_API_KEY
# is optional — its absence is fatal here. Read from backend/.env when the caller has not exported
# it, so `bin/db_instrument_types.sh both` works with no shell setup.
if [[ -z "${FMP_API_KEY:-}" ]]; then
  ENV_FILE="$ROOT/backend/.env"
  [[ -f "$ENV_FILE" ]] || { echo "✗ FMP_API_KEY not exported and $ENV_FILE is missing" >&2; exit 3; }
  FMP_API_KEY="$(grep -m1 -E '^FMP_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
  export FMP_API_KEY
fi
[[ -n "${FMP_API_KEY}" ]] || { echo "✗ FMP_API_KEY is empty" >&2; exit 3; }

LOADER_SCRIPT="/repo/db/load_instrument_types.py" exec "$ROOT/bin/db_corporate_actions.sh" "$@"
