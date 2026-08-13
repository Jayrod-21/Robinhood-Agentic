#!/usr/bin/env bash
# Pre-market daily scan — the mechanical heartbeat for the 3b Robinhood Agentic trader.
#
# Runs the free yfinance "Sprinkle Sauce" screen and writes a dated report into logs/scans/.
# This is the SCREEN ONLY. Live position review (Robinhood MCP) and the Wasden-lens reasoning
# happen in a Claude session that reads this report as the morning's starting point — because
# the Robinhood MCP is interactively authenticated and not available to a headless cron run.
#
# Installed via crontab (weekdays 07:00 America/Denver). See docs/SCHEDULING.md.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"

# Derived from this script's own location, so the repo works from any checkout on any machine.
# It was previously hard-coded to one operator's home directory, which broke for everyone else.
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || { echo "cannot cd to project"; exit 1; }

DATE="$(date +%Y-%m-%d)"
STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
OUTDIR="$PROJECT/logs/scans"
mkdir -p "$OUTDIR"
REPORT="$OUTDIR/${DATE}-premarket.md"

{
  echo "# Pre-market scan — ${STAMP}"
  echo ""
  echo "Mechanical screen of the seed universe. Survivors are pre-Wasden — the agent applies the"
  echo "forward theses (docs/THESES.md) + live positions (Robinhood MCP) in the next Claude session."
  echo ""
  echo '```'
  python3 -m src.daily_scan 2>&1
  echo '```'
  echo ""
  echo "_Next step (Claude session): re-read journal + THESES + SLATE, pull live positions for"
  echo "account 542574025, mark to market, run the morning-review lens, check stops/falsification,"
  echo "append to the journal Scan Log. Bias to NO ACTION._"
} > "$REPORT" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S %Z') wrote $REPORT"
