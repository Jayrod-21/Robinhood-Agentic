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

# A SECOND pool, on a SECOND role. Not a convenience — migration 012 REVOKEs every auth table from
# rh_app, so the pool above physically cannot read a password hash or a TOTP secret. Only rh_auth
# can, and it holds column-level grants on exactly what each auth flow touches (AUTH_THREAT_MODEL
# §8). The split is what stops a SQL injection anywhere in scan/account/debate/pipeline reaching
# the credential store.
#
# Its real limit, stated plainly because it is easy to overrate: both DSNs live in this one
# process. This defends against injection in non-auth code. It does not defend against code
# execution in the backend, and nothing here defends against host compromise.
_auth_pool: ConnectionPool | None = None
_auth_pool_lock = threading.Lock()


class DbUnavailable(RuntimeError):
    """The database cannot be used right now; the caller should degrade, not crash.

    ``reason`` is a short machine-friendly tag: 'not_configured' | 'unreachable' | 'error'.
    The message is safe to surface to the frontend — it never contains the DSN.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _create_pool(*, auth: bool = False) -> ConnectionPool | None:
    """Build a process-wide pool, or return None when its DSN is not configured.

    ``auth=True`` selects AUTH_DATABASE_URL (role rh_auth) instead of DATABASE_URL (rh_app).
    """
    settings = get_db_settings()
    dsn = settings.auth_database_url if auth else settings.database_url
    if dsn is None:
        return None

    from psycopg_pool import ConnectionPool  # deferred: see module docstring

    # check=check_connection revalidates pooled connections on checkout, so a DB restart (or the
    # nightly backup bouncing rh-db) yields a fresh connection instead of a dead-socket error
    # surfacing as a 500 on the next dashboard poll.
    return ConnectionPool(
        conninfo=dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        open=True,
        name="rh-auth" if auth else "rh-backend",
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


def get_auth_pool() -> ConnectionPool | None:
    """The process-wide rh_auth pool (created lazily), or None when AUTH_DATABASE_URL is unset."""
    global _auth_pool
    if _auth_pool is not None:
        return _auth_pool
    with _auth_pool_lock:
        if _auth_pool is None:
            pool = _create_pool(auth=True)
            if pool is None:
                return None
            _auth_pool = pool
            logger.info("auth database pool opened (min=%s max=%s)", pool.min_size, pool.max_size)
    return _auth_pool


def close_pool() -> None:
    """Close and forget both pools. Safe to call repeatedly; tests use it between DSN changes."""
    global _pool, _auth_pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception as exc:  # noqa: BLE001 — teardown must never mask the test/app error
                logger.warning("closing database pool raised: %s", exc)
            _pool = None
    with _auth_pool_lock:
        if _auth_pool is not None:
            try:
                _auth_pool.close()
            except Exception as exc:  # noqa: BLE001 — same reasoning
                logger.warning("closing auth database pool raised: %s", exc)
            _auth_pool = None


@contextmanager
def auth_connection() -> Iterator[psycopg.Connection[Any]]:
    """Acquire a pooled rh_auth connection for the authentication path only.

    THIS ONE DOES NOT DEGRADE, AND CALLERS MUST NOT MAKE IT.
        ``connection()`` above exists so the dashboard keeps serving when the database is down —
        history features go dark, the account page still renders. That is correct for reading data
        and catastrophic for deciding who is allowed in. An auth path that "degrades gracefully"
        when it cannot reach the credential store is an authentication bypass: it fails toward
        letting the request through.

        So callers of this function must translate DbUnavailable into a refusal (503, no session
        minted, no session accepted), never into a default-allow. AUTH_THREAT_MODEL §5.13 states
        this as an invariant; ``services/auth.py`` implements it in exactly one place.

    Same acquisition bound and failure translation as ``connection()`` — what differs is only the
    role, the pool, and the meaning the caller must assign to failure.
    """
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout

    pool = get_auth_pool()
    if pool is None:
        raise DbUnavailable(
            "not_configured",
            "authentication is not configured on this server",
        )

    settings = get_db_settings()
    try:
        with pool.connection(timeout=settings.db_acquire_timeout_seconds) as conn:
            yield conn
    except PoolTimeout as exc:
        logger.warning(
            "auth database unreachable: pool acquire timed out after %.1fs",
            settings.db_acquire_timeout_seconds,
        )
        raise DbUnavailable("unreachable", "authentication is temporarily unavailable") from exc
    except OperationalError as exc:
        logger.warning("auth database connection failed: %s", exc)
        raise DbUnavailable("unreachable", "authentication is temporarily unavailable") from exc


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
