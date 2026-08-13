"""Load and validate the volume-mounted account snapshot.

The snapshot is the bridge between this read-only dashboard and the real Robinhood account: the
in-session Claude agent (or the host-side refresh daemon) writes it from MCP data; we only read it.
Because it crosses a trust boundary (a file written by another process), every field is validated
with Pydantic and bad input fails loudly with a clear error rather than silently producing wrong P&L.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, NonNegativeFloat

logger = logging.getLogger("agentic.services.snapshot")

# The one snapshot schema this reader understands. The producer (bin/refresh_prompt.md) writes
# ``"schema_version": 1``; load_snapshot compares against this and refuses any other value, so a
# future producer-side schema bump fails loudly here instead of being silently misread.
SNAPSHOT_SCHEMA_VERSION = 1


class SnapshotPosition(BaseModel):
    """One held position as captured from Robinhood. Read-only; no execution fields."""

    symbol: str = Field(min_length=1, max_length=8)
    # A listed position must be a real long holding: strictly positive. A zero or negative quantity
    # in the file is a producer bug or tampering, and must fail validation, not flow into P&L math.
    quantity: float = Field(gt=0)
    average_buy_price: NonNegativeFloat
    intraday_quantity: float = 0.0


class SnapshotAccount(BaseModel):
    number_masked: str = "••••4025"
    nickname: str | None = None
    total_value: NonNegativeFloat
    equity_value: NonNegativeFloat
    # Cash account (no margin): negative cash or buying power is impossible for this account and
    # rejected as malformed input.
    cash: NonNegativeFloat
    buying_power: NonNegativeFloat
    currency: str = "USD"


class AccountSnapshot(BaseModel):
    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    source: str = "robinhood-mcp"
    generated_at: str  # ISO-8601 UTC, as written by the producer
    account: SnapshotAccount
    positions: list[SnapshotPosition]

    @property
    def symbols(self) -> list[str]:
        return [p.symbol for p in self.positions]


class SnapshotError(RuntimeError):
    """Raised when the snapshot is missing or malformed (surfaced to the client as 503)."""


def load_snapshot(path: Path) -> AccountSnapshot:
    """Read + validate the snapshot file. Raises SnapshotError on any problem."""
    if not path.exists():
        raise SnapshotError(
            f"No account snapshot at {path}. Click Refresh in the dashboard (or run the "
            f"manual sync in bin/sync_snapshot.md) to generate one from the Robinhood MCP."
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Account snapshot unreadable: {exc}") from exc
    try:
        snapshot = AccountSnapshot.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError and friends
        raise SnapshotError(f"Account snapshot failed validation: {exc}") from exc
    if snapshot.schema_version != SNAPSHOT_SCHEMA_VERSION:
        # Fail loudly: a version we don't understand means the producer changed the contract, and
        # reading it with this model could silently misinterpret every field.
        logger.error(
            "rejecting account snapshot at %s: schema_version=%s but this reader only supports %s",
            path,
            snapshot.schema_version,
            SNAPSHOT_SCHEMA_VERSION,
        )
        raise SnapshotError(
            f"Account snapshot schema_version is {snapshot.schema_version}, but this server only "
            f"supports version {SNAPSHOT_SCHEMA_VERSION}. Regenerate the snapshot with a matching "
            f"producer."
        )
    return snapshot
