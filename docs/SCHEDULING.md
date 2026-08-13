# Scheduling — the daily pre-market routine

## Why local, not a remote cloud routine
The `/schedule` skill creates **remote** agents in Anthropic's cloud. Those can't run this project:
1. **No access to the local code/docs.** 3b lives in a **private** GitHub repo
   (`Jayrod-21/Robinhood-Agentic`) — the journal holds live positions and cost basis, so it is not
   public. A cloud agent has no checkout and no credentials for it, and the live account snapshot
   is never committed at all, so the state that matters is only ever on the host.
2. **No Robinhood MCP.** The Robinhood connection is interactively authenticated and is NOT one of the
   cloud connectors — a headless remote run can't see the live book or place orders.

So scheduling lives **locally**, which is also the literal first step toward the eventual 24/7 local app.

## What's installed
A local cron job runs the mechanical screen every weekday pre-market:

```
0 7 * * 1-5   bin/morning_scan.sh      # 07:00 America/Denver (system TZ), Mon–Fri
```

`bin/morning_scan.sh` runs `python -m src.daily_scan` and writes `logs/scans/YYYY-MM-DD-premarket.md`.
That's the **screen only** — survivors pre-Wasden. The **intelligence** (live positions via the
Robinhood MCP, the forward theses, the morning-review lens, stop/falsification checks, journaling)
happens in the next **Claude session**, which opens that report as the morning's starting point.

## Reliability caveat
Cron only fires while the machine is awake. If the box is asleep at 07:00 the run is skipped —
cron does not catch up.

> **Superseded.** This section previously recommended Windows Task Scheduler driving `wsl.exe`.
> The host is now native Linux, so `schtasks` and `wsl.exe` do not exist here. Use a systemd timer
> instead, which does catch up on a missed run:

```ini
# ~/.config/systemd/user/agentic-morning-scan.timer
[Timer]
OnCalendar=Mon..Fri 07:00
Persistent=true          # runs on resume if the box was asleep at 07:00
```

Pair it with a matching `.service` unit whose `ExecStart` points at `bin/morning_scan.sh` in your
checkout, then `systemctl --user enable --now agentic-morning-scan.timer`. Optional — nothing is
scheduled today (`crontab -l` is empty and no timer is installed).

## Path to the 24/7 app
This cron is the seed. The local app/script will bundle into one running service: the scan **+** a
live Robinhood position pull **+** LLM reasoning (theses, stops, falsification) **+** journaling to the
event store (`logs/events.jsonl`, see `logs/README.md`) **+** alerting the owners — and, once trusted,
supervised order placement. Until then: cron produces the screen; a Claude session does the thinking.
