"""Refresh-script tool scope (issue #20): the Claude allow-list stays least-privilege.

These pin the ``ALLOWED_TOOLS`` arrays in both refresh scripts as text, so a future edit that
re-broadens ``Write`` (any host path) or reintroduces a loose ``Bash(date*)`` prefix fails a test
instead of shipping.
"""

import re
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[2] / "bin"
SCRIPTS = [BIN / "refresh_daemon.sh", BIN / "refresh_once.sh"]

# The one exact allow-list both scripts must carry: read-only Robinhood pulls, Write scoped to the
# snapshot file (absolute-path rule form), and the single timestamp command the prompt asks for.
EXPECTED_TOOLS = [
    "mcp__robinhood-trading__get_portfolio",
    "mcp__robinhood-trading__get_equity_positions",
    "Write(//${SNAPSHOT_FILE#/})",
    "Bash(date -u +%Y-%m-%dT%H:%M:%SZ)",
]


def _allowed_tools(script: Path) -> list[str]:
    match = re.search(r"^ALLOWED_TOOLS=\((.*?)^\)$", script.read_text(), re.DOTALL | re.MULTILINE)
    assert match, f"{script.name}: ALLOWED_TOOLS array not found"
    entries = []
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line.strip("'\""))
    return entries


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda s: s.name)
def test_allow_list_is_exactly_least_privilege(script):
    assert _allowed_tools(script) == EXPECTED_TOOLS


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda s: s.name)
def test_write_rule_targets_the_snapshot_file(script):
    # The Write rule scopes via ${SNAPSHOT_FILE}; make sure that variable really is the snapshot,
    # so the rule confines writes to what we think it confines them to.
    assert re.search(
        r'^SNAPSHOT_FILE="\$\{[A-Z_]+\}(/data)?/account_snapshot\.json"$',
        script.read_text(),
        re.MULTILINE,
    ), f"{script.name}: SNAPSHOT_FILE does not point at the account snapshot"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda s: s.name)
def test_no_permission_skip_flag(script):
    # Comments may (and do) mention the flag to document that it is deliberately not used; only a
    # non-comment line counts as usage.
    code_lines = [ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("--dangerously-skip-permissions" in ln for ln in code_lines)
