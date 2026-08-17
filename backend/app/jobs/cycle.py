"""Twice-daily cycle: scan the universe, debate each held position, write an open/close report.

Run inside the backend container (it has the engine, the API key, src/, and the mounted volumes):
    python -m app.jobs.cycle open      # or: close
The host refreshes the Robinhood snapshot file FIRST (bin/scheduled_cycle.sh → bin/refresh_once.sh),
because the Robinhood MCP lives host-side; this job then reads the account through the broker service
(services/broker.py) — a live Alpaca read when credentials are configured, otherwise that
freshly-written fallback file from the shared volume.

Each per-position debate runs the real engine directly (no HTTP, no rate limit) and persists itself to
logs/debates + logs/events.jsonl. This job adds a consolidated logs/reports/<date>-<phase>.md and a
single `cycle` event. It degrades gracefully: no account data (missing snapshot file, or Alpaca
configured but unreachable) → scan-only; no API key → skip debates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone

from app.config import get_settings

# The logging bootstrap (format + secret-redaction filter) lives in app.main so the API process
# and this cron job share one implementation; importing it here costs one extra app construction
# at job start, which is negligible next to the scan + debates the job exists to run.
from app.main import configure_logging

configure_logging()
logger = logging.getLogger("agentic.jobs.cycle")


async def _run_one_debate(ticker: str, sem: asyncio.Semaphore) -> dict:
    """Consume a full debate for one ticker; return its decision summary. Never raises."""
    from app.debate.engine import run_debate

    out = {"ticker": ticker, "decision": None, "escalated": False, "reason": None, "error": None}
    async with sem:
        try:
            async for ev in run_debate(ticker):
                kind = ev.get("type")
                if kind == "aggregate":
                    out["escalated"] = bool(ev["jury"]["escalated_to_human"])
                elif kind == "decision":
                    out["decision"] = ev["final_decision"]
                    out["reason"] = ev.get("reason")
                elif kind == "error":
                    out["error"] = ev["message"]
        except Exception as exc:  # noqa: BLE001 — one bad debate must not sink the cycle
            logger.warning("debate for %s failed: %s", ticker, exc)
            out["error"] = str(exc)
    return out


def _run_scan_sync() -> list:
    from src.daily_scan import DEFAULT_MIN_CAP, run_scan
    from src.universe import flat_universe

    return run_scan(flat_universe(), min_market_cap=DEFAULT_MIN_CAP)


def _account_view_sync():
    from app.routers.account import _build_view
    from app.services.snapshot import SnapshotError

    try:
        return _build_view()
    except SnapshotError as exc:
        logger.warning("account view unavailable: %s", exc)
        return None


def _format_report(phase: str, now: datetime, account, survivors, scanned, debates) -> str:
    lines = [f"# Cycle report — {phase.upper()} — {now:%Y-%m-%d %H:%M UTC}", ""]

    if account is not None:
        lines += [
            "## Account",
            (
                f"- Total value: ${account.live_total_value:,.2f} "
                f"(equity ${account.live_equity_value:,.2f}, cash ${account.cash:,.2f})"
            ),
            (
                f"- Unrealized P&L: ${account.total_unrealized_pl:,.2f} "
                f"({account.total_unrealized_pl_pct if account.total_unrealized_pl_pct is not None else 0:+.2f}%)"
            ),
            f"- Snapshot: {account.generated_at}" + ("  ⚠ some prices stale" if account.stale_prices else ""),
            "",
        ]
    else:
        lines += ["## Account", "- No snapshot available (fix the broker connection, or run a refresh for the fallback file).", ""]

    lines += [f"## Scan — {len(survivors)} survivors of {len(scanned)} scanned"]
    if survivors:
        for r in survivors:
            lines.append(f"- {r.ticker}  score {r.composite:.3f}")
    else:
        lines.append("- (none cleared the gates)")
    lines.append("")

    lines += ["## Position debates"]
    if not debates:
        lines.append("- (skipped — no API key or no positions)")
    else:
        for d in debates:
            if d["error"]:
                lines.append(f"- {d['ticker']}: ERROR — {d['error']}")
            else:
                flag = " ⚠ ESCALATED" if d["escalated"] else ""
                lines.append(f"- {d['ticker']}: **{d['decision']}**{flag}")
        escalations = [d["ticker"] for d in debates if d["escalated"]]
        if escalations:
            lines += ["", f"**Escalations needing a human:** {', '.join(escalations)}"]
    lines.append("")
    return "\n".join(lines)


async def run_cycle(phase: str, max_debates: int = 0) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)

    account = await asyncio.to_thread(_account_view_sync)
    scanned = await asyncio.to_thread(_run_scan_sync)
    survivors = sorted((r for r in scanned if r.passed), key=lambda r: r.composite or 0.0, reverse=True)

    debates: list[dict] = []
    if not settings.anthropic_api_key:
        logger.info("no ANTHROPIC_API_KEY — skipping position debates")
    elif account is None:
        logger.info("no account data — skipping position debates")
    else:
        symbols = [p.symbol for p in account.positions]
        if max_debates > 0:
            symbols = symbols[:max_debates]
        # Count only, no tickers: this line lands in logs/cron/ twice a day, and the symbol list
        # is the account's holdings (issue #14). The per-ticker detail lives in the report file.
        logger.info("debating %d position(s)", len(symbols))
        sem = asyncio.Semaphore(2)  # 2 debates at once (each already fans out its own jury)
        debates = await asyncio.gather(*[_run_one_debate(s, sem) for s in symbols])

    report = _format_report(phase, now, account, survivors, scanned, debates)
    report_path = settings.logs_dir / "reports" / f"{now:%Y-%m-%d}-{phase}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    event = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "cycle",
        "payload": {
            "phase": phase,
            "total_value": account.live_total_value if account else None,
            "unrealized_pl": account.total_unrealized_pl if account else None,
            "survivors": [r.ticker for r in survivors],
            "decisions": {d["ticker"]: d["decision"] for d in debates if not d["error"]},
            "escalations": [d["ticker"] for d in debates if d["escalated"]],
        },
    }
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.events_path.open("a") as fh:
        fh.write(json.dumps(event) + "\n")

    logger.info("cycle %s complete → %s", phase, report_path)
    return str(report_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Twice-daily cycle: scan + position debates + report")
    parser.add_argument("phase", choices=["open", "close"], help="market session this run covers")
    parser.add_argument("--max-debates", type=int, default=0,
                        help="cap how many positions to debate (0 = all). Useful for cheap test runs.")
    args = parser.parse_args()
    path = asyncio.run(run_cycle(args.phase, max_debates=args.max_debates))
    print(f"cycle {args.phase} → {path}")


if __name__ == "__main__":
    main()
