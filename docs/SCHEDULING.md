# Scheduling — the daily pre-market routine

## Why local, not a remote cloud routine
The `/schedule` skill creates **remote** agents in Anthropic's cloud. Those can't run this project:
1. **No access to the local code/docs.** 3b is a local git repo with no GitHub remote (kept private on
   purpose — the journal holds live positions + cost basis). A remote agent can't reach it.
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
Cron only fires while WSL2 / the machine is awake. If the box is asleep at 07:00, the run is skipped
(cron doesn't catch up). For wake-and-run robustness, use **Windows Task Scheduler** to launch it:
```
schtasks /Create /TN "RobinhoodAgentic-MorningScan" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 07:00 \
  /TR "wsl.exe -d <distro> -e bash -lc '\"/root/Jared/3b. Robinhood Agentic/bin/morning_scan.sh\"'"
```
(Set "Wake the computer to run this task" in the task's Conditions.) — optional; ask if you want it.

## Path to the 24/7 app
This cron is the seed. The local app/script will bundle into one running service: the scan **+** a
live Robinhood position pull **+** LLM reasoning (theses, stops, falsification) **+** journaling to the
event store (`logs/events.jsonl`, see `logs/README.md`) **+** alerting Jared — and, once trusted,
supervised order placement. Until then: cron produces the screen; a Claude session does the thinking.
