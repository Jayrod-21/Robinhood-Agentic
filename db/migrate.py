"""Migration runner for the 3b Postgres database.

WHY hand-rolled: Alembic presumes SQLAlchemy models and we own raw SQL deliberately (schema shape is
the point here, not an ORM mapping); Flyway drags in a JVM and Sqitch a Perl runtime. The bar was:
lives in this repo, auditable in one sitting, and usable directly from the test harness without a
subprocess. `main(argv)` is importable, so tests call it as a function.

DISCOVERY
    Applies ``NNN_<name>.up.sql`` / ``.down.sql`` pairs from ``db/migrations`` in numeric order.
    Both directions are required; a missing partner is a hard error rather than a silent skip.

GUARANTEES
    * Each migration runs inside ONE transaction, and the ``schema_migrations`` bookkeeping row is
      written in that SAME transaction — so a partially-applied migration is impossible.
    * The runner OWNS the transaction. Migration files must not contain top-level BEGIN / COMMIT /
      ROLLBACK / START TRANSACTION / SAVEPOINT; discovery REJECTS any file that does, so a stray
      COMMIT can never truncate the runner's transaction and decouple the schema change from its
      bookkeeping row. (This is not hypothetical — it is the exact bug 9b Korean Master hit and
      recorded in its ADR-013.)
    * Re-running a migration whose file changed since it was applied raises ChecksumMismatch rather
      than silently diverging from what is actually in the database.
    * ``--allow-destructive`` is required for any migration classified destructive, and ``--dry-run``
      evaluates that gate too — so a deploy's plan step aborts on a pending destructive migration
      instead of discovering it halfway through applying.
    * Migration sessions run with statement_timeout = 0. Large indexes and backfills take as long as
      they take; atomicity, not a timeout, is what protects us.

EXIT CODES
    0 success or no-op · 1 validation failure · 2 SQL execution failure · 3 connection failure
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
except ModuleNotFoundError:  # pragma: no cover - surfaced as a clear message, not a traceback
    print("migrate: psycopg (v3) is required. pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(3) from None

logger = logging.getLogger("migrate")

# ── exit codes ────────────────────────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_SQL = 2
EXIT_CONNECTION = 3

# Zero-padded so a lexical sort is also a numeric sort. At version 1000 either widen every existing
# filename to 4 digits or switch to an integer sort — do not mix widths.
FILENAME_RE = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.(?P<dir>up|down)\.sql$")

# Top-level transaction control. The runner owns the transaction (see module docstring).
TX_CONTROL_RE = re.compile(
    r"\b(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION|SAVEPOINT|RELEASE\s+SAVEPOINT)\b",
    re.IGNORECASE,
)

# Fallback destructive sniff. Deliberately does NOT match DROP INDEX / TYPE / CONSTRAINT: those are
# recreatable and lose no rows. Shapes this misses (mass DELETE FROM, DROP COLUMN) are why the
# explicit directive below exists and takes precedence.
DESTRUCTIVE_RE = re.compile(r"\b(DROP\s+TABLE|DROP\s+SCHEMA|DROP\s+DATABASE|TRUNCATE)\b", re.IGNORECASE)

DIRECTIVE_RE = re.compile(r"^\s*--\s*migrate:\s*(destructive|non-destructive)\s*$", re.IGNORECASE | re.MULTILINE)

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
    pass


class ChecksumMismatch(MigrationError):
    pass


class DestructiveBlocked(MigrationError):
    pass


class ConflictingDestructiveMarkers(MigrationError):
    pass


# ── SQL text stripping ────────────────────────────────────────────────────────────────────────
# Three levels, and the differences between them are load-bearing. Read the comment on each before
# changing which one a check uses.

def strip_sql_comments(sql: str) -> str:
    """Remove line and block comments. String literals are left intact."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def strip_sql_noise(sql: str) -> str:
    """Remove comments AND string literals (single-quoted and dollar-quoted).

    Used only by the transaction-control detector. Without stripping dollar-quoted bodies, every
    PL/pgSQL block (``DO $$ BEGIN … END $$``) would trip the BEGIN check; without stripping ordinary
    literals, ``COMMENT ON … IS 'we commit to …'`` would too.
    """
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", " ", sql, flags=re.DOTALL)
    sql = strip_sql_comments(sql)
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)
    return sql


def _strip_string_literals_only(sql: str) -> str:
    """Remove string literals but KEEP comments.

    Used by the directive scanner, so a literal containing the text ``-- migrate: non-destructive``
    cannot forge a directive. The real directive lives in a comment, which is why comments survive.
    """
    return re.sub(r"'(?:[^']|'')*'", " ", sql)


def contains_top_level_tx_control(sql: str) -> bool:
    return bool(TX_CONTROL_RE.search(strip_sql_noise(sql)))


def explicit_destructiveness(sql: str) -> bool | None:
    """True/False from an explicit directive, or None when the file declares nothing."""
    found = {m.group(1).lower() for m in DIRECTIVE_RE.finditer(_strip_string_literals_only(sql))}
    if len(found) > 1:
        raise ConflictingDestructiveMarkers(
            "file declares both 'migrate: destructive' and 'migrate: non-destructive'; "
            "there is no safe default — pick one"
        )
    if not found:
        return None
    return found.pop() == "destructive"


def is_destructive(sql: str) -> bool:
    """Explicit directive wins; otherwise sniff the body.

    The sniff deliberately does NOT strip string literals. A false positive costs one
    ``--allow-destructive`` flag; a false negative costs data. For a gate guarding data loss,
    erring toward false positives is the correct asymmetry.
    """
    declared = explicit_destructiveness(sql)
    if declared is not None:
        return declared
    return bool(DESTRUCTIVE_RE.search(strip_sql_comments(sql)))


# ── model ─────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Path

    @property
    def up_sql(self) -> str:
        return self.up_path.read_text(encoding="utf-8")

    @property
    def down_sql(self) -> str:
        return self.down_path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        """SHA-256 of the UP body only.

        The down body is not checksummed: rolling back deletes its bookkeeping row outright, so
        there is no stored state for a down-file edit to diverge from.
        """
        return hashlib.sha256(self.up_sql.encode("utf-8")).hexdigest()


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Parse, validate, and order the migration set. Raises before touching the database."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    pairs: dict[str, dict[str, Path]] = {}
    names: dict[str, str] = {}

    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        m = FILENAME_RE.match(path.name)
        if not m:
            raise MigrationError(
                f"{path.name}: does not match NNN_name.up.sql / NNN_name.down.sql "
                "(3+ digits, lowercase snake_case name)"
            )
        version, name, direction = m.group("version"), m.group("name"), m.group("dir")
        if names.setdefault(version, name) != name:
            raise MigrationError(f"version {version} used by two different names: {names[version]!r} and {name!r}")
        slot = pairs.setdefault(version, {})
        if direction in slot:
            raise MigrationError(f"duplicate {direction} file for version {version}")
        slot[direction] = path

    migrations: list[Migration] = []
    for version in sorted(pairs):
        slot = pairs[version]
        if "up" not in slot or "down" not in slot:
            missing = "down" if "up" in slot else "up"
            raise MissingPair(f"version {version} ({names[version]}) has no .{missing}.sql — both directions are required")
        mig = Migration(version=version, name=names[version], up_path=slot["up"], down_path=slot["down"])

        # Validate BOTH bodies now, so a bad file cannot reach the database even if an earlier
        # migration in the same run would have succeeded.
        for direction, sql, path in (("up", mig.up_sql, mig.up_path), ("down", mig.down_sql, mig.down_path)):
            if contains_top_level_tx_control(sql):
                raise TxControlInMigration(
                    f"{path.name}: contains top-level transaction control. The runner owns the "
                    "transaction — remove BEGIN/COMMIT/ROLLBACK/SAVEPOINT. (PL/pgSQL 'DO $$ BEGIN … END $$' is fine.)"
                )
            explicit_destructiveness(sql)  # raises on conflicting markers
            del direction
        migrations.append(mig)

    return migrations


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
    if not dsn:
        raise MigrationError(
            "DATABASE_URL is not set. Expected e.g. "
            "postgresql://user:pass@rh-db:5432/robinhood_agentic"
        )
    conn = psycopg.connect(dsn, autocommit=True, application_name="rh-migrate")
    # Session-scoped (not SET LOCAL) so they survive across each migration's own transaction. A
    # CREATE INDEX on a 300M-row table will outlive any sane statement_timeout.
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute("SET idle_in_transaction_session_timeout = 0")
    return conn


def ensure_bookkeeping(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(BOOKKEEPING_DDL)


def applied_migrations(conn: psycopg.Connection) -> dict[str, str]:
    """version -> checksum for everything already applied."""
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def apply_one(conn: psycopg.Connection, mig: Migration) -> int:
    """Apply one migration. Body + bookkeeping row commit or abort together."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT clock_timestamp()")
            started = cur.fetchone()[0]
            cur.execute(mig.up_sql)
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
            cur.execute("SELECT clock_timestamp()")
            started = cur.fetchone()[0]
            cur.execute(mig.down_sql)
            cur.execute("SELECT clock_timestamp()")
            finished = cur.fetchone()[0]
            duration_ms = int((finished - started).total_seconds() * 1000)
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (mig.version,))
    return duration_ms


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
    # rather than partway through applying.
    blocked = [m for m in pending if is_destructive(m.up_sql)]
    if blocked and not allow_destructive:
        names = ", ".join(f"{m.version}_{m.name}" for m in blocked)
        raise DestructiveBlocked(
            f"destructive migration(s) pending: {names}. "
            "Re-run with --allow-destructive once you have confirmed the data loss is intended."
        )

    for mig in pending:
        tag = " (DESTRUCTIVE)" if is_destructive(mig.up_sql) else ""
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

    # A down body is destructive by nature, so the gate applies to every rollback, not just to
    # bodies that happen to trip the sniff.
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
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="migrate", description="3b database migration runner")
    p.add_argument("command", choices=("status", "up", "down"))
    p.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path(__file__).parent / "migrations",
        help="directory holding NNN_name.{up,down}.sql (default: db/migrations)",
    )
    p.add_argument("--dry-run", action="store_true", help="print the plan; never opens a write transaction")
    p.add_argument("--allow-destructive", action="store_true", help="required for destructive migrations and all rollbacks")
    p.add_argument("--target", help="up: apply through this version inclusive. down: roll back everything above it.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        migrations = discover_migrations(args.migrations_dir)
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
        with conn:
            if args.command == "status":
                return cmd_status(conn, migrations)
            if args.command == "up":
                return cmd_up(
                    conn, migrations,
                    dry_run=args.dry_run,
                    allow_destructive=args.allow_destructive,
                    target=args.target,
                )
            return cmd_down(
                conn, migrations,
                dry_run=args.dry_run,
                allow_destructive=args.allow_destructive,
                target=args.target,
            )
    except MigrationError as exc:
        logger.error("%s", exc)
        return EXIT_VALIDATION
    except psycopg.Error as exc:
        logger.error("migration failed: %s", exc)
        return EXIT_SQL
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
