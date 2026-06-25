# Manual account snapshot sync (fallback)

The dashboard's **Refresh** button normally drives this automatically (button → backend writes
`data/refresh.request` → `bin/refresh_daemon.sh` → a `claude` tab connects the Robinhood MCP and
rewrites `data/account_snapshot.json`). Use this manual path only when the daemon isn't running.

## Why this can't run inside the container
The `robinhood-trading` MCP is an OAuth server scoped to the host Claude session (`/root/Jared`).
A Docker container has no access to it, so the snapshot is always produced **host-side** by a real
`claude` process and read by the container from the shared `./data` volume.

## One-shot manual refresh (host shell, as root)
Run from `/root/Jared` so the project-scoped MCP loads. Prompt first, `--allowedTools` last
(it's variadic). `--dangerously-skip-permissions` is intentionally avoided (root-blocked); the
specific read tools + `Write` + `date` are pre-authorized instead:

```bash
cd /root/Jared
claude --print "$(cat '/root/Jared/3b. Robinhood Agentic/bin/refresh_prompt.md')" \
  --allowedTools mcp__robinhood-trading__get_portfolio \
                 mcp__robinhood-trading__get_equity_positions \
                 Write 'Bash(date*)'
```

Success prints `DONE` and rewrites `data/account_snapshot.json` with a fresh `generated_at`. The
dashboard's `/api/account` picks it up on the next poll. If the Robinhood OAuth token has expired,
run `claude` interactively from `/root/Jared` once and approve the re-auth, then re-run the above.

## In-session (this Claude) alternative
This session already has the MCP authenticated, so it can regenerate the snapshot directly with
`get_portfolio` + `get_equity_positions` for account `542574025` and write the same schema.
