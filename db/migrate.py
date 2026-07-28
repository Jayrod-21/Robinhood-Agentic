"""Migration runner for the 3b Postgres database.

WHY hand-rolled: Alembic presumes SQLAlchemy models and we own raw SQL deliberately (schema shape is
the point here, not an ORM mapping); Flyway drags in a JVM and Sqitch a Perl runtime. The bar was:
lives in this repo, auditable in one sitting, and usable directly from the test harness without a
subprocess. `main(argv)` is importable, so tests call it as a function.

DISCOVERY
    Applies ``NNN_<name>.up.sql`` / ``NNN_<name>.down.sql`` pairs from ``db/migrations`` in numeric
    order. Both directions are required; a missing partner is a hard error rather than a silent
    skip. A migration that destroys data is marked IN ITS FILENAME:
    ``NNN_<name>.destructive.up.sql`` / ``NNN_<name>.destructive.down.sql`` — each direction is
    marked independently. Every file's text is read ONCE at discovery and carried on the Migration
    object — the SQL that is validated and checksummed is the SQL that executes.

DESTRUCTIVE CLASSIFICATION — the filename is the single source of truth
    Three independent verification rounds (docs/fixpass/REVIEW_migrate_runner.md, REVIEW_FIXES.md,
    REVIEW_FIXES_b1_residual.md) forged the predecessor design, which read a ``-- migrate:``
    directive out of the SQL text and therefore had to reimplement PostgreSQL's lexer in Python to
    tell comments from literals from code. Thirteen distinct end-to-end forgeries later, the parser
    is GONE (ADR-002). A filename cannot be influenced by anything inside the file, so the entire
    forgery class is structurally impossible: no text a migration body contains can change its
    classification. ``-- migrate:`` comments are inert, and none remain in the tree: the dead
    directive lines were removed from the 001-003 up bodies during the 004 fix-pass (2026-07-28),
    which cycled the schema-only, never-shipped database to 000 and re-applied it, re-recording
    all four checksums (ADR-002, Consequences). The checksum rule itself is unchanged: editing an
    applied migration without re-applying it still raises ChecksumMismatch.

    A keyword sniff backs the filename as a BEST-EFFORT secondary net: a file whose RAW TEXT
    contains DROP TABLE / DROP SCHEMA / DROP DATABASE / DROP OWNED / DROP MATERIALIZED / TRUNCATE
    — keywords separated by whitespace or by SQL comments, which PostgreSQL's lexer treats as
    token separators — but whose filename is not marked destructive is REFUSED at discovery. The
    sniff deliberately does NOT strip comments or literals first ('DROP TABLE' in a comment
    refuses too; the fix is a one-line rename or rewording), and it is deliberately INCOMPLETE:
    it has never covered mass DELETE FROM or DROP COLUMN, it does not see nested block-comment
    separators, and NO text rule can decide dynamically built SQL — EXECUTE 'DR'||'OP TABLE ...'
    destroys data without containing any keyword (round 4, REVIEW_redesign_verification.md
    R4-B1). The author marking the filename correctly is the real control; the sniff only
    reduces the cost of forgetting it, for the common literal shapes.

GUARANTEES
    * Each migration runs inside ONE transaction, and the ``schema_migrations`` bookkeeping row is
      written in that SAME transaction — so a partially-applied-but-recorded migration is
      impossible.
    * The runner OWNS the transaction. This is enforced by the SERVER, not by parsing SQL: after
      the body executes and before the bookkeeping row is written, the runner asserts that
      libpq still reports an open transaction (``conn.info.transaction_status`` == INTRANS) AND
      that ``pg_catalog.pg_current_xact_id()`` still returns the xid captured when the runner's
      transaction started. A stray COMMIT or ROLLBACK leaves the connection idle (status check); a
      ``COMMIT; BEGIN;`` pair forges INTRANS but cannot forge the xid. All four shapes verified
      against a live PG16. On detection the runner aborts WITHOUT recording the migration — but
      statements the hijacking COMMIT already committed cannot be un-committed; the error says so
      and names the file. A bare ``BEGIN`` or ``SAVEPOINT`` inside a body is tolerated: verified
      live, both leave the runner's transaction and xid intact (BEGIN inside a transaction is a
      server-side no-op warning), so atomicity is unharmed.
    * Files are read as BYTES and rejected at discovery if they contain a NUL byte (libpq
      transports the query as a C string, so everything after a NUL would silently not execute
      while the checksum covers the whole file — verified in round 3), start with a UTF-8 BOM
      (PostgreSQL rejects it as an identifier, but with a confusing server-side error), or are not
      valid UTF-8.
    * Re-running a migration whose file changed since it was applied raises ChecksumMismatch rather
      than silently diverging from what is actually in the database.
    * ``--allow-destructive`` is required for any migration whose filename is marked destructive,
      and ``--dry-run`` evaluates that gate too — so a deploy's plan step aborts on a pending
      destructive migration instead of discovering it halfway through applying.
    * Concurrent runners serialize on a Postgres advisory lock (session-scoped, released on
      disconnect) instead of racing to confusing mid-deploy SQL errors.
    * Migration sessions run with statement_timeout = 0. Large indexes and backfills take as long as
      they take; atomicity, not a timeout, is what protects us.

LIMITATION
    Because each migration runs inside a transaction, statements that refuse to run in one —
    ``CREATE INDEX CONCURRENTLY``, ``VACUUM`` — cannot appear in a migration. At this database's
    size that is fine; if CIC is ever needed, it needs a separate out-of-band path, not a migration.

EXIT CODES
    0 success or no-op · 1 validation failure (including CLI usage errors and a detected
    transaction hijack) · 2 SQL execution failure · 3 connection failure
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import psycopg
    from psycopg.pq import TransactionStatus
except ModuleNotFoundError:  # pragma: no cover - surfaced as a clear message, not a traceback
    print("migrate: psycopg (v3) is required. pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("migrate")

# ── exit codes ────────────────────────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_SQL = 2
EXIT_CONNECTION = 3

# Serializes concurrent runners (two `up`s racing plan the same pending set and die mid-deploy on
# duplicate-key errors otherwise). Session-scoped: released automatically when the connection
# closes, even on a crash. The constant is arbitrary but must never change: it is the lock
# identity. (ASCII "RHMIGR01" as a 64-bit int.)
MIGRATION_LOCK_KEY = 0x5248_4D49_4752_3031

# THE single source of truth for a migration's identity AND destructiveness. Version is zero-padded
# so a lexical sort is also a numeric sort — discovery REJECTS a mixed-width set (e.g. 999 alongside
# 1000) because lexical order would silently diverge from numeric order; at version 1000, widen
# every existing filename to 4 digits in one commit. The name charset excludes '.', so the
# `.destructive` marker cannot be smuggled into (or faked by) a name — `001_x.destructiv.up.sql`
# and `001_x.up.destructive.sql` both fail to match and are rejected loudly.
FILENAME_RE = re.compile(
    # \Z, not $: in Python `$` also matches before a trailing newline, so "002_x.up.sql\n" would
    # pass the grammar (round 4 NIT-1).
    r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)(?P<destructive>\.destructive)?\.(?P<dir>up|down)\.sql\Z"
)

# Discovery must never SKIP a plausible migration silently (round 4 R4-S2: an uppercase `.SQL`
# file was dropped by a `.suffix != ".sql"` check, so `up` printed "no pending migrations" and
# exited 0 while the migration never ran). Anything version-prefixed is treated as an intended
# migration and must match FILENAME_RE or die loudly.
_VERSION_PREFIX_RE = re.compile(r"\d+_")

# BEST-EFFORT secondary net behind the filename marker: these keywords in a file NOT marked
# destructive refuse the whole run at discovery. Keywords may be separated by whitespace OR by SQL
# comments — `drop/**/table` is a valid DROP TABLE because PostgreSQL's lexer treats a comment as
# a token separator (round 4 R4-B1) — so _SEP mirrors that: whitespace, `-- …` line comments, and
# one level of `/* … */` block comments. Searched on the RAW text on purpose — no comment or
# literal stripping, so 'DROP TABLE' in a comment or string over-fires, and the fix is a rename or
# rewording. Deliberately does NOT match DROP INDEX / TYPE / CONSTRAINT / VIEW / ROLE
# (recreatable, no stored rows lost). KNOWN HOLES, accepted on purpose: mass DELETE FROM, DROP
# COLUMN, nested block-comment separators (PostgreSQL block comments nest; a regex cannot), and
# dynamically built SQL (EXECUTE 'DR'||'OP TABLE …'), which no text rule can decide. Those shapes
# are gated ONLY by the author marking the filename — this net just cheapens forgetting to.
_SEP = r"(?:[ \t\r\n\f\v]|--[^\n]*(?:\n|$)|/\*(?:[^*]|\*(?!/))*\*/)+"
DESTRUCTIVE_SNIFF_RE = re.compile(
    rf"\b(?:DROP{_SEP}(?:TABLE|SCHEMA|DATABASE|OWNED|MATERIALIZED)|TRUNCATE)\b", re.IGNORECASE
)

BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by  TEXT        NOT NULL,
    duration_ms INTEGER     NOT NULL
)
"""


class MigrationError(Exception):
    """Base for every failure this module raises deliberately."""


class MissingPair(MigrationError):
    pass


class TxControlInMigration(MigrationError):
    """A migration body committed or rolled back the runner's transaction (detected post-hoc)."""


class ChecksumMismatch(MigrationError):
    pass


class DestructiveBlocked(MigrationError):
    pass


class UnmarkedDestructiveSql(MigrationError):
    """Destructive keywords in a file whose filename does not carry the `.destructive` marker."""


# ── model ─────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Path
    # From the FILENAME only — nothing inside a file can influence these (ADR-002).
    up_destructive: bool
    down_destructive: bool
    # Read once at discovery. The text validated and checksummed is the text executed — a file
    # edited between discovery and execution cannot swap unvalidated SQL into the transaction.
    up_sql: str
    down_sql: str
    checksum: str  # SHA-256 of the UP body. The down body is not checksummed: rolling back deletes
    # its bookkeeping row outright, so there is no stored state for a down-edit to diverge from.


def _read_sql_file(path: Path) -> str:
    """Read one migration file, rejecting byte-level hazards before any analysis.

    * NUL byte: libpq transports the query as a C string, so PostgreSQL would execute only the text
      BEFORE the first NUL while the runner checksums and records the whole file — a silent
      partial apply (demonstrated end-to-end in round 3, REVIEW_FIXES_b1_residual.md NEW2-S2).
    * UTF-8 BOM: PostgreSQL does not skip it; the server error ("syntax error at or near ...") is
      cryptic, so refuse it here with a message that names the actual problem.
    * Invalid UTF-8: turned into a MigrationError so the CLI reports one clean line, not a
      UnicodeDecodeError traceback.
    """
    data = path.read_bytes()
    nul_at = data.find(b"\x00")
    if nul_at != -1:
        raise MigrationError(
            f"{path.name}: contains a NUL byte at offset {nul_at}. libpq would silently "
            "truncate the query there, executing only part of the file — remove it."
        )
    if data.startswith(b"\xef\xbb\xbf"):
        raise MigrationError(
            f"{path.name}: starts with a UTF-8 BOM, which PostgreSQL rejects as a stray "
            "character — save the file without a BOM."
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"{path.name}: not valid UTF-8 ({exc})") from None


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Parse, validate, and order the migration set. Raises before touching the database."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    paths: dict[str, dict[str, Path]] = {}
    destructive: dict[str, dict[str, bool]] = {}
    names: dict[str, str] = {}

    for path in sorted(migrations_dir.iterdir()):
        m = FILENAME_RE.match(path.name)
        if m is None:
            # Round 4 R4-S2: a near-miss dropped here silently makes `up` report "no pending
            # migrations" and exit 0 while the SQL never runs — success reported, schema change
            # lost. So anything that plausibly WANTS to be a migration (a version-like prefix, or
            # a .sql-like extension in any case / with trailing dot-space-newline junk) refuses
            # the run loudly. The grammar is all-lowercase and exact everywhere else (name
            # charset, marker); accepting `.SQL` case-insensitively would fork the grammar and
            # invite `002_x.up.sql` + `002_x.up.SQL` ambiguity, so near-misses are rejected, not
            # adopted. Genuinely unrelated entries (README.md, subdirectories) are still ignored.
            if path.is_dir():
                continue
            sql_like = path.name.casefold().rstrip(" .\t\r\n").endswith(".sql")
            if sql_like or _VERSION_PREFIX_RE.match(path.name):
                raise MigrationError(
                    f"{path.name}: does not match NNN_name[.destructive].up.sql / "
                    "NNN_name[.destructive].down.sql (3+ digits, lowercase snake_case name, "
                    "lowercase .sql). Refusing to skip it: a silently ignored file would let "
                    "`up` report success while this migration never runs."
                )
            continue
        if not path.is_file():
            # A directory or dangling symlink NAMED like a migration would previously be skipped
            # by an is_file() check — the same silent-success failure mode as above. Refuse.
            raise MigrationError(
                f"{path.name}: matches the migration filename grammar but is not a regular "
                "file — refusing to skip it silently."
            )
        version, name, direction = m.group("version"), m.group("name"), m.group("dir")
        if names.setdefault(version, name) != name:
            raise MigrationError(f"version {version} used by two different names: {names[version]!r} and {name!r}")
        slot = paths.setdefault(version, {})
        if direction in slot:
            raise MigrationError(f"duplicate {direction} file for version {version}")
        slot[direction] = path
        destructive.setdefault(version, {})[direction] = m.group("destructive") is not None

    # Mixed widths would make the lexical sort diverge from numeric order ("1000" < "999"
    # lexically) and silently apply migrations out of order. A comment cannot enforce that; this
    # check does.
    widths = {len(v) for v in paths}
    if len(widths) > 1:
        raise MigrationError(
            f"mixed version widths {sorted(widths)} — lexical order would diverge from numeric "
            "order. Widen every existing filename to the larger width in one commit."
        )

    migrations: list[Migration] = []
    for version in sorted(paths):
        slot = paths[version]
        if "up" not in slot or "down" not in slot:
            missing = "down" if "up" in slot else "up"
            raise MissingPair(f"version {version} ({names[version]}) has no .{missing}.sql — both directions are required")
        up_sql = _read_sql_file(slot["up"])
        down_sql = _read_sql_file(slot["down"])

        # Best-effort sniff, BOTH directions, before the database is ever touched. The filename is
        # the classification; this net only refuses the mislabelings it can see (the common
        # literal shapes — see DESTRUCTIVE_SNIFF_RE for the holes it deliberately has).
        for sql, path, marked in (
            (up_sql, slot["up"], destructive[version]["up"]),
            (down_sql, slot["down"], destructive[version]["down"]),
        ):
            hit = DESTRUCTIVE_SNIFF_RE.search(sql)
            if hit and not marked:
                stem = path.name.removesuffix(".sql")
                direction = stem.rsplit(".", 1)[-1]
                # A comment-separated hit can span an arbitrarily long comment; keep the message
                # one readable line.
                matched = hit.group(0) if len(hit.group(0)) <= 60 else hit.group(0)[:60] + "…"
                raise UnmarkedDestructiveSql(
                    f"{path.name}: contains {matched!r} but its filename is not marked "
                    f"destructive. Rename it to {version}_{names[version]}.destructive."
                    f"{direction}.sql (or reword, if the match is only in a comment or string — "
                    "this check reads the raw text and refuses on purpose: a false positive "
                    "costs a rename, a false negative costs data)."
                )

        migrations.append(
            Migration(
                version=version,
                name=names[version],
                up_path=slot["up"],
                down_path=slot["down"],
                up_destructive=destructive[version]["up"],
                down_destructive=destructive[version]["down"],
                up_sql=up_sql,
                down_sql=down_sql,
                checksum=hashlib.sha256(up_sql.encode("utf-8")).hexdigest(),
            )
        )

    return migrations


def validate_target(target: str | None, command: str, migrations: list[Migration]) -> str | None:
    """Reject a --target that is not a discovered version (plus the all-zeros sentinel on down).

    Versions are zero-padded strings compared lexically; an unpadded target ("2") would silently
    over-apply on up and silently no-op on down. Fail loudly instead of either.
    """
    if target is None or command == "status":
        return None
    versions = [m.version for m in migrations]
    width = len(versions[0]) if versions else 3
    allowed = set(versions)
    if command == "down":
        allowed.add("0" * width)  # sentinel: roll back everything
    if target not in allowed:
        raise MigrationError(
            f"invalid --target {target!r}: must be one of {', '.join(sorted(allowed))} "
            "(zero-padded, exactly as in the filenames)"
        )
    return target


# ── database helpers ──────────────────────────────────────────────────────────────────────────
def _runner_principal() -> str:
    import getpass
    import socket

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — getpass fails in containers with no passwd entry
        user = os.environ.get("USER", "unknown")
    return f"{user}@{socket.gethostname()}"


def connect_from_env() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if dsn is None:
        if os.environ.get("PGHOST"):
            # libpq assembles the connection from PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.
            # Preferred by bin/db_migrate.sh: no URL string means no percent-encoding hazards from
            # credentials containing '@', '/', or '%'.
            dsn = ""
        else:
            raise MigrationError(
                "no connection configured: set DATABASE_URL, or PGHOST (+PGUSER/PGPASSWORD/"
                "PGDATABASE) for libpq-style env configuration"
            )
    raw_timeout = os.environ.get("MIGRATE_CONNECT_TIMEOUT", "10")
    try:
        connect_timeout = int(raw_timeout)
    except ValueError:
        raise MigrationError(f"MIGRATE_CONNECT_TIMEOUT must be an integer, got {raw_timeout!r}") from None
    # connect_timeout: libpq's default is wait-forever; a wedged-but-routable host must fail the
    # runner (and any deploy script above it) in seconds, not hang it indefinitely.
    conn = psycopg.connect(
        dsn, autocommit=True, application_name="rh-migrate", connect_timeout=connect_timeout
    )
    try:
        # Session-scoped (not SET LOCAL) so they survive across each migration's own transaction. A
        # CREATE INDEX on a 300M-row table will outlive any sane statement_timeout — this
        # deliberately overrides the server default (60s, set in docker-compose.db.yml).
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute("SET idle_in_transaction_session_timeout = 0")
            # Serialize concurrent runners; blocks until the peer finishes. See MIGRATION_LOCK_KEY.
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
    except psycopg.Error:
        conn.close()  # do not leak the connection when session setup fails
        raise
    return conn


def ensure_bookkeeping(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(BOOKKEEPING_DDL)


def applied_migrations(conn: psycopg.Connection) -> dict[str, str]:
    """version -> checksum for everything already applied."""
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def _assert_tx_intact(conn: psycopg.Connection, cur: psycopg.Cursor, filename: str, xid_before: int) -> None:
    """Server-side proof that the runner's transaction survived the migration body.

    Called after the body executes, BEFORE the bookkeeping row is written. Two checks, both from
    the backend rather than from any reading of the SQL text (all verified live on PG16):

    * ``transaction_status`` must be INTRANS. A stray COMMIT or ROLLBACK leaves the connection
      IDLE (both observed live). INERROR cannot be observed here — a failing statement raises out
      of ``cur.execute`` before this function runs — but any non-INTRANS status fails the check.
    * ``pg_catalog.pg_current_xact_id()`` must equal the xid captured when the runner's
      transaction started. ``COMMIT; BEGIN;`` restores INTRANS but was observed live to change the
      xid — the status check alone would pass, this one cannot. Schema-qualified so a search_path
      change inside the body cannot shadow it with a user function. A bare BEGIN or SAVEPOINT
      keeps both status and xid intact (observed live) and is therefore tolerated: neither breaks
      the body+bookkeeping atomicity this check protects.

    Raising here aborts without writing the bookkeeping row; psycopg's transaction-block exit then
    rolls back whatever transaction is open (verified: after COMMIT;BEGIN, the hijacker's second
    transaction is rolled back; after a stray COMMIT the connection is idle and the exit rollback
    is a harmless no-op). Statements committed by the hijacking COMMIT itself are already durable
    and CANNOT be undone — the error message says so.
    """
    status = TransactionStatus(conn.info.transaction_status)
    same_xid = False
    if status == TransactionStatus.INTRANS:
        cur.execute("SELECT pg_catalog.pg_current_xact_id()")
        same_xid = cur.fetchone()[0] == xid_before
    if status != TransactionStatus.INTRANS or not same_xid:
        raise TxControlInMigration(
            f"{filename}: the migration issued its own transaction control (COMMIT/ROLLBACK — "
            f"post-body status {status.name}, runner transaction "
            f"{'replaced' if status == TransactionStatus.INTRANS else 'gone'}). "
            "The runner owns the transaction; nothing was recorded in schema_migrations, but any "
            "statements the stray COMMIT already committed are durable — inspect the database "
            "and clean up manually before re-running."
        )


def apply_one(conn: psycopg.Connection, mig: Migration) -> int:
    """Apply one migration. Body + bookkeeping row commit or abort together."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.pg_current_xact_id(), clock_timestamp()")
            xid_before, started = cur.fetchone()
            cur.execute(mig.up_sql)
            _assert_tx_intact(conn, cur, mig.up_path.name, xid_before)
            cur.execute("SELECT clock_timestamp()")
            finished = cur.fetchone()[0]
            duration_ms = int((finished - started).total_seconds() * 1000)
            cur.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_by, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s)",
                (mig.version, mig.name, mig.checksum, _runner_principal(), duration_ms),
            )
    return duration_ms


def rollback_one(conn: psycopg.Connection, mig: Migration) -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.pg_current_xact_id(), clock_timestamp()")
            xid_before, started = cur.fetchone()
            cur.execute(mig.down_sql)
            _assert_tx_intact(conn, cur, mig.down_path.name, xid_before)
            cur.execute("SELECT clock_timestamp()")
            finished = cur.fetchone()[0]
            duration_ms = int((finished - started).total_seconds() * 1000)
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (mig.version,))
    return duration_ms


def _warn_orphans(applied: dict[str, str], migrations: list[Migration]) -> None:
    """A row in schema_migrations with no file in the repo: loud on every command, not only status."""
    orphans = sorted(set(applied) - {m.version for m in migrations})
    if orphans:
        logger.warning(
            "applied migration(s) with no file in the repo: %s — the database contains a change "
            "nobody can reproduce or roll back (see `status`)",
            ", ".join(orphans),
        )


# ── commands ──────────────────────────────────────────────────────────────────────────────────
def cmd_status(conn: psycopg.Connection, migrations: list[Migration]) -> int:
    ensure_bookkeeping(conn)
    applied = applied_migrations(conn)

    print(f"{'version':<10} {'name':<40} {'state':<10} checksum")
    for mig in migrations:
        if mig.version in applied:
            ok = applied[mig.version] == mig.checksum
            print(f"{mig.version:<10} {mig.name:<40} {'applied':<10} {'ok' if ok else 'MISMATCH'}")
        else:
            print(f"{mig.version:<10} {mig.name:<40} {'pending':<10} -")

    # A row whose file has vanished is a real problem: the database contains a change nobody can
    # reproduce or roll back. Surface it rather than quietly ignoring the extra row.
    known = {m.version for m in migrations}
    for version in sorted(set(applied) - known):
        print(f"{version:<10} {'(file missing)':<40} {'applied':<10} ORPHAN")
    return EXIT_OK


def cmd_up(
    conn: psycopg.Connection,
    migrations: list[Migration],
    *,
    dry_run: bool,
    allow_destructive: bool,
    target: str | None,
) -> int:
    ensure_bookkeeping(conn)
    applied = applied_migrations(conn)
    _warn_orphans(applied, migrations)

    # Checksum every already-applied migration before planning anything. An edited applied file
    # means the database and the repo disagree, and nothing after this point is trustworthy.
    for mig in migrations:
        if mig.version in applied and applied[mig.version] != mig.checksum:
            raise ChecksumMismatch(
                f"{mig.up_path.name} changed since it was applied "
                f"(recorded {applied[mig.version][:12]}…, file {mig.checksum[:12]}…). "
                "Revert the file or write a new migration — never edit an applied one."
            )

    pending = [m for m in migrations if m.version not in applied]
    if target is not None:
        pending = [m for m in pending if m.version <= target]
    if not pending:
        logger.info("no pending migrations")
        return EXIT_OK

    # Evaluate the destructive gate at PLAN time, including on --dry-run, so a deploy aborts here
    # rather than partway through applying. Classification is the filename, nothing else.
    blocked = [m for m in pending if m.up_destructive]
    if blocked and not allow_destructive:
        names = ", ".join(f"{m.version}_{m.name}" for m in blocked)
        raise DestructiveBlocked(
            f"destructive migration(s) pending: {names}. "
            "Re-run with --allow-destructive once you have confirmed the data loss is intended."
        )

    for mig in pending:
        tag = " (DESTRUCTIVE)" if mig.up_destructive else ""
        if dry_run:
            print(f"would apply: {mig.version}_{mig.name}{tag}")
            continue
        logger.info("applying %s_%s%s", mig.version, mig.name, tag)
        duration = apply_one(conn, mig)
        logger.info("applied %s_%s in %dms", mig.version, mig.name, duration)
    return EXIT_OK


def cmd_down(
    conn: psycopg.Connection,
    migrations: list[Migration],
    *,
    dry_run: bool,
    allow_destructive: bool,
    target: str | None,
) -> int:
    ensure_bookkeeping(conn)
    applied = applied_migrations(conn)
    _warn_orphans(applied, migrations)

    candidates = [m for m in migrations if m.version in applied]
    if not candidates:
        logger.info("nothing to roll back")
        return EXIT_OK

    # Bare `down` rolls back exactly one; `--target N` rolls back everything strictly above N.
    to_revert = sorted(candidates, key=lambda m: m.version, reverse=True)
    to_revert = [m for m in to_revert if m.version > target] if target is not None else to_revert[:1]
    if not to_revert:
        logger.info("nothing to roll back above target %s", target)
        return EXIT_OK

    # A down body is destructive by nature (it discards the schema its up created, and any data in
    # it), so the gate applies to EVERY rollback — not only to downs whose filename is marked.
    if not allow_destructive:
        names = ", ".join(f"{m.version}_{m.name}" for m in to_revert)
        raise DestructiveBlocked(
            f"rollback drops schema and any data in it: {names}. Re-run with --allow-destructive."
        )

    for mig in to_revert:
        if dry_run:
            print(f"would roll back: {mig.version}_{mig.name}")
            continue
        logger.info("rolling back %s_%s", mig.version, mig.name)
        duration = rollback_one(conn, mig)
        logger.info("rolled back %s_%s in %dms", mig.version, mig.name, duration)
    return EXIT_OK


# ── entrypoint ────────────────────────────────────────────────────────────────────────────────
class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors, which the exit-code contract reserves for SQL failures.

    A deploy script branching on exit codes must never read a typo as "go look at the database" —
    usage errors are validation failures, exit 1.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_VALIDATION)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="migrate", description="3b database migration runner")
    p.add_argument("command", choices=("status", "up", "down"))
    p.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path(__file__).parent / "migrations",
        help="directory holding NNN_name[.destructive].{up,down}.sql (default: db/migrations)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without applying it (still creates the schema_migrations bookkeeping table if absent)",
    )
    p.add_argument("--allow-destructive", action="store_true", help="required for destructive migrations and all rollbacks")
    p.add_argument("--target", help="up: apply through this version inclusive. down: roll back everything above it.")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits: 0 for --help, EXIT_VALIDATION for usage errors
        return int(exc.code or 0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        migrations = discover_migrations(args.migrations_dir)
        target = validate_target(args.target, args.command, migrations)
    except MigrationError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION

    try:
        conn = connect_from_env()
    except MigrationError as exc:
        logger.error("%s", exc)
        return EXIT_CONNECTION
    except psycopg.Error as exc:
        logger.error("could not connect: %s", exc)
        return EXIT_CONNECTION

    try:
        # `with conn:` closes the connection on both success and exception paths (psycopg3) — no
        # separate finally needed, and close() releases the advisory lock.
        with conn:
            if args.command == "status":
                return cmd_status(conn, migrations)
            if args.command == "up":
                return cmd_up(
                    conn, migrations,
                    dry_run=args.dry_run,
                    allow_destructive=args.allow_destructive,
                    target=target,
                )
            return cmd_down(
                conn, migrations,
                dry_run=args.dry_run,
                allow_destructive=args.allow_destructive,
                target=target,
            )
    except MigrationError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.Error as exc:
        logger.error("migration failed: %s", exc)
        return EXIT_SQL


if __name__ == "__main__":
    raise SystemExit(main())
