"""Twice-daily cycle: scan the universe, debate each held position, write an open/close report.

Run inside the backend container (it has the engine, the API key, src/, and the mounted volumes):
    python -m app.jobs.cycle open      # or: close

The account comes through services/broker.py: a live Alpaca read when credentials are configured,
otherwise the fallback file that bin/alpaca_sync.sh keeps current. The host step used to be a
Robinhood MCP pull via bin/refresh_once.sh — that bridge is gone, and with Alpaca configured this
job does not depend on the file at all.

WHAT A RUN COSTS
    One debate per held position, and each debate fans out a jury. At fifteen positions that is
    ~195 API calls per cycle. `cycle_max_debates` (Parameters page) caps it without a
    redeploy, and every debate record now carries its own token usage so the spend is auditable
    rather than a guess.

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
from app.services import cycle_state, reconcile_check

configure_logging()
logger = logging.getLogger("agentic.jobs.cycle")


async def _run_one_debate(ticker: str, sem: asyncio.Semaphore) -> dict:
    """Consume a full debate for one ticker; return its decision summary. Never raises."""
    from app.debate.engine import run_debate

    out = {"ticker": ticker, "decision": None, "escalated": False, "reason": None, "error": None,
           "usage": None}
    async with sem:
        try:
            async for ev in run_debate(ticker):
                kind = ev.get("type")
                if kind == "aggregate":
                    out["escalated"] = bool(ev["jury"]["escalated_to_human"])
                elif kind == "decision":
                    out["decision"] = ev["final_decision"]
                    out["reason"] = ev.get("reason")
                elif kind == "debate_complete":
                    # The record carries what this debate actually spent.
                    out["usage"] = (ev.get("record") or {}).get("usage")
                elif kind == "error":
                    out["error"] = ev["message"]
        except Exception as exc:  # noqa: BLE001 — one bad debate must not sink the cycle
            logger.warning("debate for %s failed: %s", ticker, exc)
            out["error"] = str(exc)
    return out


def _run_scan_sync() -> list:
    from app.services import settings_store
    from src.daily_scan import DEFAULT_MIN_CAP, run_scan
    from src.universe import flat_universe

    # The same tuned floor the interactive scan uses. Hardcoding the default here meant the cycle
    # and the Scan page could screen the universe against different gates and both call it "the
    # scan" — with nothing on either result saying which floor produced it.
    min_cap = settings_store.get_or("screen_min_market_cap_b", DEFAULT_MIN_CAP / 1e9) * 1e9
    return run_scan(flat_universe(), min_market_cap=min_cap)


def _account_view_sync():
    from app.routers.account import _build_view
    from app.services.snapshot import SnapshotError

    try:
        return _build_view()
    except SnapshotError as exc:
        logger.warning("account view unavailable: %s", exc)
        return None


def _format_report(
    phase: str, now: datetime, account, survivors, scanned, debates, reconciliation=None
) -> str:
    lines = [f"# Cycle report — {phase.upper()} — {now:%Y-%m-%d %H:%M UTC}", ""]

    # First, above the account and the debates. It is the only section that changes how every other
    # section should be read.
    lines += reconcile_check.report_section(reconciliation)

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
    spend = [d["usage"] for d in debates if d.get("usage")]
    if spend:
        lines.append(
            f"- Spend: {sum(u['calls'] for u in spend)} API calls, "
            f"{sum(u['input_tokens'] for u in spend):,} in / "
            f"{sum(u['output_tokens'] for u in spend):,} out tokens"
        )
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
    """Run one cycle, recording that it ran.

    The progress row is opened here and closed here, including on the way out through an exception:
    a crashed cycle that never closes its row reports as in progress until the stale sweep catches
    it 90 minutes later, which answers "is it running?" wrongly and confidently. Recording the
    failure immediately is the difference between a page that says "failed at 09:34 — FMP timeout"
    and one that says "running" about a process that is not.
    """
    # Opened before any work, so a cycle that dies during the scan is still visible as one that
    # started and did not finish — rather than as nothing having happened at all.
    run_id = await asyncio.to_thread(cycle_state.start, phase)
    try:
        return await _run_cycle_body(phase, max_debates, run_id)
    except Exception as exc:
        # The message, not the traceback: this lands on an operator's screen. The traceback is
        # already in the log via the caller.
        await asyncio.to_thread(
            cycle_state.finish, run_id, error=f"{type(exc).__name__}: {exc}"[:500]
        )
        raise


async def _run_cycle_body(phase: str, max_debates: int, run_id: int | None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)

    # PREFLIGHT, before anything else spends a token. Debating eight positions against a slate that
    # no longer describes the book is not a cheaper mistake for being made first — it is the same
    # mistake with a bill attached, and it is what happened twice a day for weeks (issue #22).
    reconciliation = await asyncio.to_thread(reconcile_check.run)
    # Record BEFORE honouring a halt. The run an operator most wants explained is the one that
    # refused to proceed, and it must not be the one that stored nothing about why.
    await asyncio.to_thread(cycle_state.record_reconciliation, run_id, reconciliation)
    if reconciliation.get("halt"):
        raise reconcile_check.DesyncHalt(reconciliation["halt"])

    account = await asyncio.to_thread(_account_view_sync)
    scanned = await asyncio.to_thread(_run_scan_sync)
    survivors = sorted((r for r in scanned if r.passed), key=lambda r: r.composite or 0.0, reverse=True)
    await asyncio.to_thread(
        cycle_state.update, run_id, scanned=len(scanned), survivors=len(survivors)
    )

    debates: list[dict] = []
    if not settings.anthropic_api_key:
        logger.info("no ANTHROPIC_API_KEY — skipping position debates")
    elif account is None:
        logger.info("no account data — skipping position debates")
    else:
        symbols = [p.symbol for p in account.positions]
        # The cap is a tunable, so cost can be pulled back without a redeploy. An explicit
        # --max-debates still wins: a command-line run is someone testing, and a stored setting
        # should not silently override what they typed.
        if max_debates <= 0:
            try:
                from app.services import settings_store

                max_debates = int(settings_store.get("cycle_max_debates"))
            except Exception as exc:  # noqa: BLE001 — a settings failure must not skip the cycle
                logger.warning("could not read cycle_max_debates, debating all positions: %s", exc)
                max_debates = 0
        if max_debates > 0:
            symbols = symbols[:max_debates]
        # Count only, no tickers: this line lands in logs/cron/ on every run, and the symbol list
        # is the account's holdings (issue #14). The per-ticker detail lives in the report file.
        logger.info("debating %d position(s)", len(symbols))
        await asyncio.to_thread(cycle_state.update, run_id, total_positions=len(symbols))
        sem = asyncio.Semaphore(2)  # 2 debates at once (each already fans out its own jury)

        # Progress is recorded as each debate FINISHES, not as it is dispatched: two run at a time,
        # so a count of what has been started would sit at 2 for eighty seconds and then jump. What
        # an operator wants to know is how much is done.
        done = 0
        lock = asyncio.Lock()

        async def _tracked(symbol: str) -> dict:
            nonlocal done
            result = await _run_one_debate(symbol, sem)
            async with lock:
                done += 1
                await asyncio.to_thread(
                    cycle_state.update, run_id, completed_positions=done, current_symbol=symbol
                )
            return result

        debates = await asyncio.gather(*[_tracked(s) for s in symbols])

    report = _format_report(phase, now, account, survivors, scanned, debates, reconciliation)
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

    await asyncio.to_thread(cycle_state.finish, run_id)
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
