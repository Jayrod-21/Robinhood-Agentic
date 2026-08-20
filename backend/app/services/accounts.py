"""Which brokerage accounts this deployment can read, and the credentials for each.

WHY A REGISTRY
    Everything here assumed one account. The broker cache was a single module-level tuple with no
    account dimension, and the credentials came from two bare environment variables. Adding a second
    account without this would mean one account's holdings served under another's name — the worst
    failure this dashboard could have, because every number on the page would be real and belong to
    someone else.

CREDENTIALS STAY IN THE ENVIRONMENT
    Not a database table. Storing five API secrets in Postgres means either plaintext secrets in a
    table that backups copy, or building a second encryption scheme beside the one the auth store
    already has. backend/.env is mode 0600, gitignored, and already holds the key this deployment
    uses. A profiles table would still need the secrets somewhere; this keeps one secret store.

    ALPACA_ACCOUNT_<N>_NAME / _KEY_ID / _SECRET_KEY / _BASE_URL, for N in 1..9.

BACKWARD COMPATIBLE BY CONSTRUCTION
    Account 1 falls back to the bare ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY / ALPACA_BASE_URL
    that this deployment uses today. A config with no numbered accounts keeps working unchanged and
    reports exactly one account, which is what it has.

THE LIST NEVER CARRIES A SECRET
    `describe()` returns id, name, and whether the endpoint is paper. Not the key id — that is a
    credential half, and a dashboard that displays it teaches an operator it is safe to paste.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("agentic.services.accounts")

# How many numbered profiles to look for. Nine is arbitrary and generous: Joe named five, and a
# bound exists so a typo'd variable name cannot become an account nobody meant to configure.
_MAX_ACCOUNTS = 9

DEFAULT_ACCOUNT_ID = 1


@dataclass(frozen=True)
class AccountProfile:
    id: int
    name: str
    key_id: str
    secret_key: str
    base_url: str | None

    @property
    def is_paper(self) -> bool:
        """Decided by the URL, because the URL is what routes the request.

        A key that "looks like" a paper key is a guess. This mirrors AlpacaClient.assert_paper so
        the list and the client can never disagree about which endpoint an account points at.
        """
        return "paper-api" in (self.base_url or "").lower() or not self.base_url

    def describe(self) -> dict[str, object]:
        """The safe projection. No key material, ever."""
        return {"id": self.id, "name": self.name, "is_paper": self.is_paper}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _profile(index: int) -> AccountProfile | None:
    """One numbered profile, or None when it is not configured.

    Account 1 inherits the unnumbered variables when its own are absent, so an existing deployment
    needs no config change to keep working.
    """
    key_id = _env(f"ALPACA_ACCOUNT_{index}_KEY_ID")
    secret = _env(f"ALPACA_ACCOUNT_{index}_SECRET_KEY")
    base_url = _env(f"ALPACA_ACCOUNT_{index}_BASE_URL")
    name = _env(f"ALPACA_ACCOUNT_{index}_NAME")

    if index == DEFAULT_ACCOUNT_ID:
        key_id = key_id or _env("ALPACA_API_KEY_ID")
        secret = secret or _env("ALPACA_API_SECRET_KEY")
        base_url = base_url or _env("ALPACA_BASE_URL")

    if not key_id or not secret:
        # Half a credential is a misconfiguration, not an account. Listing it would put a name in
        # the switcher that 401s the moment anyone selects it.
        if key_id or secret:
            logger.warning(
                "account %d has only half a credential (key_id=%s, secret=%s) and is not listed",
                index, bool(key_id), bool(secret),
            )
        return None

    return AccountProfile(
        id=index,
        name=name or f"Account {index}",
        key_id=key_id,
        secret_key=secret,
        base_url=base_url or None,
    )


def profiles() -> list[AccountProfile]:
    """Every configured account, in id order.

    Read from the environment on each call rather than cached: this is a handful of dict lookups,
    and a cached registry would need a restart to pick up a credential an operator just added —
    exactly the kind of "why isn't it showing up" that costs an hour.
    """
    return [p for i in range(1, _MAX_ACCOUNTS + 1) if (p := _profile(i)) is not None]


def get_profile(account_id: int | None) -> AccountProfile | None:
    """The profile for an id, or the default when omitted. None when it is not configured."""
    wanted = DEFAULT_ACCOUNT_ID if account_id is None else int(account_id)
    for profile in profiles():
        if profile.id == wanted:
            return profile
    return None


def any_configured() -> bool:
    return bool(profiles())
