"""Database settings, separate from ``app.config.Settings`` on purpose.

The DSN convention matches ``db/migrate.py``: a single ``DATABASE_URL`` env var. It is optional —
an unset/empty value means "no database", which is a supported, first-class state (the dashboard
ran for months without one). Nothing in this module opens a socket; it only describes how to.

Security note: the DSN carries the ``rh_app`` password, so it must never be logged or serialized
into an API response. ``db_health()`` reports booleans and role/version strings only.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DbSettings(BaseSettings):
    """Field names map to upper-cased env vars, same convention as ``app.config.Settings``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # libpq URL, e.g. postgresql://rh_app:...@rh-db:5432/rh — same variable db/migrate.py reads.
    # None (or empty, see the validator) = no database configured; every consumer degrades.
    database_url: str | None = Field(default=None)

    # min_size=0 is the graceful-degradation setting: the pool opens instantly with zero
    # connections, so an unreachable database costs nothing at startup and only surfaces when a
    # DB-backed request actually tries to acquire (and then fails within the bounded timeout).
    db_pool_min_size: int = Field(default=0, ge=0, le=10)
    db_pool_max_size: int = Field(default=4, ge=1, le=32)

    # Both timeouts are deliberately short. This is a dashboard: a hung page is worse than an
    # honest 503, and db_health() must answer fast even when the network path is black-holed.
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=60)
    db_acquire_timeout_seconds: float = Field(default=2.0, ge=0.1, le=60.0)

    db_application_name: str = Field(default="rh-backend")

    @field_validator("database_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, v: object) -> object:
        # docker-compose passes ${DATABASE_URL:-} which is "" when unset — treat as absent so
        # "configured" checks agree instead of seeing a non-None empty string (same idiom as
        # app.config's anthropic_api_key validator).
        if isinstance(v, str) and not v.strip():
            return None
        return v


@lru_cache(maxsize=1)
def get_db_settings() -> DbSettings:
    """Cached singleton so the pool and every consumer see the same config."""
    return DbSettings()


def reset_db_settings() -> None:
    """Drop the cached settings (tests change DATABASE_URL between cases)."""
    get_db_settings.cache_clear()
