# Manual account snapshot sync (fallback)

The dashboard's **Refresh** button normally drives this automatically (button → backend writes
`data/refresh.request` → `bin/refresh_daemon.sh` → a `claude` tab connects the Robinhood MCP and
rewrites `data/account_snapshot.json`). Use this manual path only when the daemon isn't running.

## Why this can't run inside the container
The `robinhood-trading` MCP is an OAuth server scoped to a **host** Claude session. A Docker
container has no access to it, so the snapshot is always produced host-side by a real `claude`
process and read by the container from the shared `./data` volume.

Note this is a genuine single-operator dependency, not just a path detail: whoever holds the
authenticated MCP session is the only person who can refresh. See issue #20 and the security
notes before assuming a second owner can run this.

## One-shot manual refresh (host shell)
Run from the directory the `robinhood-trading` MCP is registered in — any directory if it was added
with `--scope user`, otherwise the project root it was scoped to. Prompt first, `--allowedTools`
last (it's variadic). `--dangerously-skip-permissions` is intentionally avoided; the specific read
tools plus `Write` and `date` are pre-authorized instead:

```bash
REPO="$(git -C . rev-parse --show-toplevel)"   # or cd to your checkout
claude --print "$(cat "${REPO}/bin/refresh_prompt.md")" \
  --allowedTools mcp__robinhood-trading__get_portfolio \
                 mcp__robinhood-trading__get_equity_positions \
                 Write 'Bash(date*)'
```

Success prints `DONE` and rewrites `data/account_snapshot.json` with a fresh `generated_at`. The
dashboard's `/api/account` picks it up on the next poll. If the Robinhood OAuth token has expired,
run `claude` interactively once, approve the re-auth, then re-run the above.

## In-session alternative
Any Claude session with the `robinhood-trading` MCP already authenticated can regenerate the
snapshot directly with `get_portfolio` + `get_equity_positions` and write the same schema. Use the
account number recorded in `bin/refresh_prompt.md` — the tracked issue for moving it out of the
repo and into configuration is #2 in the working task list, and until that lands it is still
written in these files.
