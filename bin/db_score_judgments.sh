#!/usr/bin/env bash
# db_score_judgments.sh — grade the debate calls whose horizon has elapsed.
#
# THE PROJECT'S OWN STATED PURPOSE
#   README: "The project is not done when it makes money. It is done when a good decision and a
#   lucky one are distinguishable." Measured 2026-08-27: 2,354 judgments on record, 22 of them ever
#   scored. The scorer existed and was careful; nothing ran it. This is that job.
#
#   Until it runs, every change to the debate engine — anchored confidence, a second model family,
#   more rebuttal rounds — is unmeasurable. We can make the debate different but not demonstrably
#   better.
#
#   bin/db_score_judgments.sh --dry-run    # report what would be graded, write nothing
#   bin/db_score_judgments.sh              # grade everything mature
#
# WHAT IT WILL NOT DO
#   Score an unfinished window, a window with a missing bar, or an ESCALATED call. See the module
#   docstring: an unscored judgment is absent from calibration, which the contract treats as
#   excluded — never as a wrong answer.
#
# Needs the database only; no provider egress. Delegates to db_corporate_actions.sh anyway, because
# that script already assembles DATABASE_URL from db/.env and passes it through the environment
# rather than argv — a second copy of that is a second place for a password to reach `ps`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOADER_SCRIPT="/repo/db/score_judgments.py" exec "$ROOT/bin/db_corporate_actions.sh" "$@"
