"""Postgres connection layer for the dashboard backend (issue #6).

The database (rh-db) lives on the ``rh-internal`` Docker network with no host port (ADR-001).
This package is therefore built around one hard requirement: the dashboard MUST keep serving
when the database is unreachable or simply not configured — DB-backed features degrade to a
clear "unavailable" state instead of taking the account/scan/debate pages down with them.

Public surface:
    get_db_settings / reset_db_settings — settings-driven DSN + pool tunables
    connection()                        — pooled psycopg connection (context manager)
    db_health()                         — structured health report, never raises
    DbUnavailable                       — the one exception callers handle for degradation
    close_pool()                        — teardown hook (tests, shutdown)
"""

from __future__ import annotations

from app.db.config import DbSettings, get_db_settings, reset_db_settings
from app.db.pool import DbUnavailable, close_pool, connection, db_health

__all__ = [
    "DbSettings",
    "DbUnavailable",
    "close_pool",
    "connection",
    "db_health",
    "get_db_settings",
    "reset_db_settings",
]
