"""Populate `securities.security_type` and `name`, and close the gap holes that form explains.

WHAT THIS FIXES (issue #41)
    `securities` is "everything that traded" — 19,745 rows derived from the whole US tape, with
    security_type, name and exchange populated on nineteen of them. So there is no universe filter,
    and a screen or a Testing Lab training run reads SPAC warrants and unexercised rights as though
    they were companies.

TWO REQUESTS, NOT 19,745
    The issue records the fix as blocked on "~19.7k calls, not free-tier feasible". That is true of
    /stable/profile and false of the plan as a whole: /stable/stock-list and /stable/etf-list are
    bulk. The whole classification is two requests, which is why this is a loader and not a project.

WHAT IT WILL NOT DO
    Populate `exchange` or `sector`. The bulk lists carry symbol and company name only, and writing
    a guessed exchange would be worse than the NULL that is there now — DATA_INVENTORY.md S-S7
    already warns that these columns are empty, and a half-filled column that looks authoritative
    is the failure this project keeps finding.

COMMANDS
    classify        Fetch both lists, classify every security, write security_type and name.
    disposition     Re-disposition gap holes whose security is not common stock or an ETF.
    both            classify, then disposition. What the runner script calls.

Every command supports --dry-run, which reports exactly what it would change and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Imported after the sys.path insert above, same as the other loaders in this directory.
from instrument_class import (
    INVESTABLE,
    classify,
    provider_symbols,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("load_instrument_types")

FMP_BASE = "https://financialmodelingprep.com/stable"
TIMEOUT_SECONDS = 120

# The disposition a hole gets when the security's FORM explains the provider's silence. Terminal:
# a delisted warrant has no provider history, so the absence is the expected answer rather than an
# unresolved question. Added to the CHECK by migration 025.
NON_COMMON = "non_common_instrument"

# Only these are re-dispositioned. 'pending_review' is deliberately NOT included: it means the
# ratio evidence found a discontinuity nothing explains, and instrument form does not answer that
# — a warrant can still have a real unexplained price break.
REDISPOSITION_FROM = ("provider_unresolvable",)


class LoadError(RuntimeError):
    pass


def connect() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise LoadError("DATABASE_URL is not set")
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-instrument-types")
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
    return conn


def _fetch(path: str, api_key: str) -> list[dict]:
    """One bulk list. Raises rather than returning a partial universe.

    A truncated list would classify real companies as `untracked` and quietly drop them out of the
    investable universe — a failure that looks exactly like a successful run with fewer companies
    in the market, which is not a thing that happens.
    """
    url = f"{FMP_BASE}/{path}?apikey={api_key}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LoadError(f"could not fetch {path}: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise LoadError(f"{path} returned no rows — refusing to classify against an empty list")
    logger.info("%s: %d symbols", path, len(payload))
    return payload


def _lists(api_key: str) -> tuple[dict[str, str], set[str]]:
    """(symbol -> company name) from stock-list, and the set of ETF symbols."""
    stock = _fetch("stock-list", api_key)
    etfs = _fetch("etf-list", api_key)

    names = {
        r["symbol"]: (r.get("companyName") or "").strip()
        for r in stock
        if r.get("symbol")
    }
    etf_symbols = {r["symbol"] for r in etfs if r.get("symbol")}
    # etf-list is a subset of stock-list on this plan, but that is the provider's arrangement and
    # not a guarantee. Merging the names keeps an ETF's name if the subset relation ever changes.
    for r in etfs:
        if r.get("symbol") and r["symbol"] not in names:
            names[r["symbol"]] = (r.get("name") or "").strip()
    return names, etf_symbols


def cmd_classify(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    return _classify(conn, args)[0]


def _classify(conn: psycopg.Connection, args: argparse.Namespace) -> tuple[int, dict[str, str]]:
    """Returns (exit code, symbol -> security_type) so a dry run can preview what follows it."""
    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        raise LoadError("FMP_API_KEY is not set")

    names, etf_symbols = _lists(api_key)
    stock_symbols = set(names)

    with conn.cursor() as cur:
        cur.execute("SELECT id, symbol, security_type, name FROM securities ORDER BY symbol")
        rows = cur.fetchall()

    updates: list[tuple[str, str | None, int]] = []
    tally: Counter[str] = Counter()
    computed: dict[str, str] = {}
    for security_id, symbol, current_type, current_name in rows:
        # Both spellings. Our archive writes BRK.B; FMP writes BRK-B. Checking only ours missed
        # every share class — 1 of 57 carried a name before this.
        spellings = provider_symbols(symbol)
        kind = classify(
            symbol,
            in_etf_list=any(v in etf_symbols for v in spellings),
            in_stock_list=any(v in stock_symbols for v in spellings),
        )
        tally[kind] += 1
        computed[symbol] = kind
        # Only a name the provider actually supplied. An empty string is not a name, and writing
        # one would make `count(name)` report coverage the table does not have.
        name = next((names[v] for v in spellings if names.get(v)), None)
        if kind != current_type or (name and name != current_name):
            updates.append((kind, name or current_name, security_id))

    total = len(rows)
    logger.info("classified %d securities:", total)
    for kind, count in tally.most_common():
        mark = "  investable" if kind in INVESTABLE else ""
        logger.info("  %-12s %6d  %5.1f%%%s", kind, count, count / total * 100, mark)
    investable = sum(tally[k] for k in INVESTABLE)
    logger.info(
        "investable universe: %d of %d (%.1f%%) — %d excluded",
        investable, total, investable / total * 100, total - investable,
    )

    if args.dry_run:
        logger.info("DRY RUN — %d rows would be updated, nothing written", len(updates))
        return 0, computed

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE securities SET security_type = %s, name = %s, updated_at = now() WHERE id = %s",
            updates,
        )
    logger.info("updated %d rows", len(updates))
    return 0, computed


def cmd_disposition(
    conn: psycopg.Connection,
    args: argparse.Namespace,
    computed: dict[str, str] | None = None,
) -> int:
    """Close gap holes whose security's FORM explains the provider's silence.

    Deliberately narrow. It moves holes only OUT of the dispositions in REDISPOSITION_FROM, only
    for securities classified as something other than common stock or an ETF, and only to a single
    terminal disposition that records why. It never touches a hole on a real company.

    `computed` is the classification `classify` just worked out. It exists for one reason: under
    `both --dry-run`, nothing has been written, so reading security_type from the table returns the
    NULLs that were there before — and the preview reported "116 holes remain" for a real run that
    closes 83 of them. A dry run that understates its own effect is worse than no dry run, because
    it is the thing an operator reads before deciding to write.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.symbol, s.security_type
              FROM price_gap_audit a
              JOIN securities s ON s.id = a.security_id
             WHERE a.disposition = ANY(%s)
             ORDER BY a.symbol
            """,
            (list(REDISPOSITION_FROM),),
        )
        holes = cur.fetchall()

    def kind_of(symbol: str, stored: str | None) -> str | None:
        return computed.get(symbol, stored) if computed else stored

    candidates = [
        (hole_id, symbol, kind)
        for hole_id, symbol, stored in holes
        if (kind := kind_of(symbol, stored)) is not None and kind not in INVESTABLE
    ]
    surviving = len(holes) - len(candidates)

    by_type = Counter(row[2] for row in candidates)
    logger.info("%d hole(s) explained by instrument form:", len(candidates))
    for kind, count in by_type.most_common():
        logger.info("  %-12s %4d", kind, count)
    # The number that matters. These are holes on REAL companies that instrument form does not
    # explain, and they stay non-terminal so check 7 keeps failing until a human dispositions them.
    logger.info(
        "%d hole(s) remain unresolved on common stock or ETFs — these are NOT closed by this tool",
        surviving,
    )

    if args.dry_run:
        logger.info("DRY RUN — nothing written")
        return 0
    if not candidates:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE price_gap_audit SET disposition = %s,"
            " evidence = coalesce(evidence || ' | ', '') || %s, updated_at = now() WHERE id = %s",
            [
                (
                    NON_COMMON,
                    (
                        f"instrument form is {kind}; a delisted {kind} has no provider history, so "
                        "the absence is the expected answer for this form rather than an identity "
                        "break (issue #41)"
                    ),
                    hole_id,
                )
                for hole_id, _symbol, kind in candidates
            ],
        )
    logger.info("dispositioned %d hole(s) as %s", len(candidates), NON_COMMON)
    return 0


def cmd_both(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    rc, computed = _classify(conn, args)
    # The in-memory classification is handed forward so a dry run previews the REAL effect. On a
    # live run it is the same answer the table now holds, so passing it changes nothing.
    return rc or cmd_disposition(conn, args, computed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["classify", "disposition", "both"])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args(argv)

    try:
        conn = connect()
    except LoadError as exc:
        logger.error("%s", exc)
        return 3
    try:
        return {"classify": cmd_classify, "disposition": cmd_disposition, "both": cmd_both}[
            args.command
        ](conn, args)
    except LoadError as exc:
        logger.error("%s", exc)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
