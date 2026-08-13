"""Pooled psycopg connections with graceful degradation (issue #6).

Design constraints, in order:

1. **The dashboard serves without the database.** No DSN, DB down, network path missing — none of
   these may crash startup or hang a request. The pool opens with zero connections
   (``min_size=0``), so nothing here blocks until a caller actually needs the DB, and then it
   fails within a bounded timeout as :class:`DbUnavailable` — the one exception routers translate
   to an honest 503.
2. **Failures are loud and specific** (SENIOR_ENGINEER_BAR §7.2: never silently block). Every
   degradation path logs why, and ``db_health()`` reports the state as data.
3. **The DSN is a secret** (it carries the rh_app password). It is never logged and never appears
   in :class:`DbUnavailable` messages or health output.

Transactions: ``connection()`` yields a psycopg connection inside the pool's context manager,
which commits on clean exit and rolls back on exception — a multi-statement write (e.g. exit
update + knowledge-base insert in ``services/outcomes.py``) is atomic by construction.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from app.db.config import get_db_settings

if TYPE_CHECKING:  # psycopg is imported lazily below so import cost/absence never breaks startup
    import psycopg
    from psycopg_pool import ConnectionPool

logger = logging.getLogger("agentic.db")

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class DbUnavailable(RuntimeError):
    """The database cannot be used right now; the caller should degrade, not crash.

    ``reason`` is a short machine-friendly tag: 'not_configured' | 'unreachable' | 'error'.
    The message is safe to surface to the frontend — it never contains the DSN.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _create_pool() -> ConnectionPool | None:
    """Build the process-wide pool, or return None when no DSN is configured."""
    settings = get_db_settings()
    if settings.database_url is None:
        return None

    from psycopg_pool import ConnectionPool  # deferred: see module docstring

    # check=check_connection revalidates pooled connections on checkout, so a DB restart (or the
    # nightly backup bouncing rh-db) yields a fresh connection instead of a dead-socket error
    # surfacing as a 500 on the next dashboard poll.
    return ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        open=True,
        name="rh-backend",
        check=ConnectionPool.check_connection,
        kwargs={
            "application_name": settings.db_application_name,
            "connect_timeout": settings.db_connect_timeout_seconds,
        },
    )


def get_pool() -> ConnectionPool | None:
    """The process-wide pool (created lazily), or None when DATABASE_URL is not set."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            pool = _create_pool()
            if pool is None:
                return None
            _pool = pool
            logger.info("database pool opened (min=%s max=%s)", pool.min_size, pool.max_size)
    return _pool


def close_pool() -> None:
    """Close and forget the pool. Safe to call repeatedly; tests use it between DSN changes."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception as exc:  # noqa: BLE001 — teardown must never mask the test/app error
                logger.warning("closing database pool raised: %s", exc)
            _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection[Any]]:
    """Acquire a pooled connection, translating every failure mode to :class:`DbUnavailable`.

    Commit/rollback semantics come from the pool's own context manager: commit on clean exit,
    rollback if the body raises. Acquisition is bounded by ``db_acquire_timeout_seconds`` so a
    dead network path costs a fast, explicit failure — never a hung request handler.
    """
    from psycopg import OperationalError  # deferred import, same reasoning as _create_pool
    from psycopg_pool import PoolTimeout

    pool = get_pool()
    if pool is None:
        raise DbUnavailable(
            "not_configured",
            "no database configured (DATABASE_URL is not set); history features are offline",
        )

    settings = get_db_settings()
    try:
        with pool.connection(timeout=settings.db_acquire_timeout_seconds) as conn:
            yield conn
    except PoolTimeout as exc:
        # The classic ADR-001 failure: the backend container is not on rh-internal, so every
        # connection attempt black-holes/refuses and the pool never has a connection to hand out.
        logger.warning("database unreachable: pool acquire timed out after %.1fs", settings.db_acquire_timeout_seconds)
        raise DbUnavailable(
            "unreachable",
            "database unreachable (connection attempt timed out); the dashboard remains available",
        ) from exc
    except OperationalError as exc:
        logger.warning("database connection failed: %s", exc)
        raise DbUnavailable(
            "unreachable",
            "database connection failed; the dashboard remains available",
        ) from exc


def db_health() -> dict[str, Any]:
    """Structured DB status for the health endpoint. NEVER raises — health must always answer.

    Reports presence booleans and schema state only; no DSN, no password, no host names.
    """
    settings = get_db_settings()
    report: dict[str, Any] = {
        "configured": settings.database_url is not None,
        "reachable": False,
        "role": None,
        "schema_version": None,
        "error": None,
    }
    if not report["configured"]:
        report["error"] = "DATABASE_URL is not set"
        return report

    try:
        with connection() as conn:
            report["reachable"] = True
            report["role"] = conn.execute("SELECT current_user").fetchone()[0]
            try:
                row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
                report["schema_version"] = row[0]
            except Exception as exc:  # noqa: BLE001 — rh_app may lack SELECT on the bookkeeping table
                # Not fatal: the runner deliberately does not grant schema_migrations to rh_app.
                conn.rollback()
                logger.debug("schema_migrations not readable: %s", exc)
                report["schema_version"] = "unknown (schema_migrations not readable by this role)"
    except DbUnavailable as exc:
        report["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — health reporting must degrade, never 500
        logger.warning("db health probe failed: %s", exc)
        report["error"] = f"health probe failed: {type(exc).__name__}"
    return report
