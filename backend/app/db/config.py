"""Database settings, separate from ``app.config.Settings`` on purpose.

The DSN convention matches ``db/migrate.py``: ``DATABASE_URL`` env var(s). Two DSNs, two roles
(``docs/AUTH_THREAT_MODEL.md`` §8):

* ``DATABASE_URL`` — role ``rh_app``, everything except authentication. Migration 012 REVOKEs the
  auth tables from it entirely, so a SQL injection or over-broad ``SELECT`` in any non-auth code
  path cannot read password hashes, TOTP secrets, or recovery-code hashes.
* ``AUTH_DATABASE_URL`` — role ``rh_auth``, the auth path's second pool and nothing else. Holds
  column-level grants on exactly the auth tables and no privileges on market data.

Both are optional — an unset/empty value means "no database", which is a supported, first-class
state (the dashboard ran for months without one). Nothing in this module opens a socket; it only
describes how to. Both roles ship passwordless from their migrations and cannot authenticate over
the network until an operator sets passwords (see ``backend/.env.example``).

Security note: each DSN carries its role's password, so neither may ever be logged or serialized
into an API response. ``db_health()`` reports booleans and role/version strings only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, anchored to THIS file rather than left as a relative ".env". A relative path is
# resolved against the process CWD, which made configuration position-dependent: a server (or the
# test suite) started from backend/ silently loaded backend/.env while the same command from the
# repo root loaded nothing — the same process asserting two different auth postures depending on
# where it was launched from. In the container this resolves to /app/.env, which the image does
# not ship (.dockerignore excludes every .env); compose-provided environment variables take
# precedence over the dotenv file either way.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class DbSettings(BaseSettings):
    """Field names map to upper-cased env vars, same convention as ``app.config.Settings``."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # libpq URL, e.g. postgresql://rh_app:...@rh-db:5432/robinhood_agentic — same variable
    # db/migrate.py reads. None (or empty, see the validator) = no database configured; every
    # consumer degrades.
    database_url: str | None = Field(default=None)

    # The auth path's DSN, role rh_auth (AUTH_THREAT_MODEL §8 step 3: a second pool that auth
    # queries go through and nothing else does). Same optionality contract as database_url — but
    # note the asymmetry documented in §8: when auth SHIPS and this is unset or unreachable,
    # authenticated routes must fail CLOSED (deny), not degrade. That fail-closed behavior lives
    # with the auth service, not here; this module only says whether a DSN was provided.
    auth_database_url: str | None = Field(default=None)

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

    @field_validator("database_url", "auth_database_url", mode="before")
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
