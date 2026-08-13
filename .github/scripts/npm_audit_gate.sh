#!/usr/bin/env bash
# npm_audit_gate.sh — npm audit as a gate that can only ratchet tighter (issue #15).
#
# A bare `npm audit --audit-level=high` is permanently red today: the frontend's pinned `next`
# has known high advisories whose only fix is a breaking major upgrade, which is tracked work.
# A permanently red gate trains people to ignore it (see the gosu note in image-scan.yml), and a
# gate dropped to `--audit-level=critical` never reports highs at all. So, same pattern as the
# image scan's narrow exclusion: every KNOWN high/critical advisory is pinned by GHSA id in
# .github/npm-audit-baseline.txt, and this script fails on any high/critical advisory NOT in that
# baseline. The baseline is SHRINK-ONLY — prune ids as upgrades land; never add one to silence a
# new finding (fix the dependency instead).
#
# Usage: bash .github/scripts/npm_audit_gate.sh   (from anywhere inside the repo; needs npm + jq)

set -Eeuo pipefail

cd "$(git rev-parse --show-toplevel)"
readonly BASELINE_FILE=".github/npm-audit-baseline.txt"

command -v jq >/dev/null 2>&1 || { echo "✗ jq not found" >&2; exit 2; }
[[ -f "${BASELINE_FILE}" ]] || { echo "✗ missing ${BASELINE_FILE}" >&2; exit 2; }

audit_json="$(mktemp)"
trap 'rm -f "${audit_json}"' EXIT

# npm audit exits nonzero when it finds anything, so capture output and judge the JSON ourselves.
# A registry/network failure yields an `error` object instead of `vulnerabilities` — treat that as
# a hard failure rather than a silent pass.
(cd frontend && npm audit --json || true) > "${audit_json}"
if ! jq -e 'has("vulnerabilities")' "${audit_json}" >/dev/null 2>&1; then
  echo "✗ npm audit did not produce a vulnerability report (registry unreachable?):" >&2
  head -c 2000 "${audit_json}" >&2
  exit 2
fi

found="$(jq -r '[.vulnerabilities[].via[] | objects
                 | select(.severity == "high" or .severity == "critical") | .url]
                | unique | .[]' "${audit_json}" | sed 's|.*/||' | sort -u)"
baseline="$(grep -v -E '^\s*(#|$)' "${BASELINE_FILE}" | sort -u)"

new="$(comm -23 <(printf '%s\n' "${found}") <(printf '%s\n' "${baseline}") | sed '/^$/d')"
stale="$(comm -13 <(printf '%s\n' "${found}") <(printf '%s\n' "${baseline}") | sed '/^$/d')"

if [[ -n "${stale}" ]]; then
  printf 'NOTE baseline advisories no longer reported — prune them from %s:\n' "${BASELINE_FILE}"
  printf '%s\n' "${stale}" | sed 's/^/  /'
fi

if [[ -n "${new}" ]]; then
  echo "✗ npm audit: NEW high/critical advisories not in the baseline:"
  while IFS= read -r id; do
    printf '  %s  https://github.com/advisories/%s\n' "${id}" "${id}"
    # Name the offending package(s) for each new advisory.
    jq -r --arg url "https://github.com/advisories/${id}" \
      '.vulnerabilities[] | select([.via[] | objects | .url] | index($url)) | "    in: " + .name' \
      "${audit_json}"
  done <<< "${new}"
  echo "  → upgrade the affected package. Do NOT add the id to ${BASELINE_FILE} — the baseline only shrinks."
  exit 1
fi

count="$(printf '%s\n' "${found}" | sed '/^$/d' | wc -l)"
echo "✓ npm audit: no high/critical advisories beyond the ${count} baselined (tracked for the next major upgrade)"
