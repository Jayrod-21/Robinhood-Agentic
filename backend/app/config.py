"""Runtime configuration, loaded from the environment (and ``backend/.env`` for local dev).

Every tunable lives here so the rest of the app never reaches into ``os.environ`` directly. The
Anthropic key is the one true secret: it is read here, used only by the debate engine, and is never
serialized into any API response or exposed to the frontend.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Field names map to upper-cased env vars (``ANTHROPIC_API_KEY`` …)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anthropic (live debate engine) ---------------------------------------------------
    # Optional so the rest of the dashboard (account, scan) runs without a key; the debate
    # endpoints fail closed with a clear 503 when it is missing rather than crashing on import.
    anthropic_api_key: str | None = Field(default=None)
    jury_model: str = Field(default="claude-haiku-4-5")

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, v: object) -> object:
        # docker-compose passes ${ANTHROPIC_API_KEY:-} which is "" when unset — treat as absent so
        # readiness checks (and the debate gate) agree instead of seeing a non-None empty string.
        if isinstance(v, str) and not v.strip():
            return None
        return v
    synth_model: str = Field(default="claude-sonnet-4-6")
    jury_size: int = Field(default=10, ge=3, le=20)
    debate_max_concurrency: int = Field(default=10, ge=1, le=20)

    # --- Account / Robinhood ----------------------------------------------------------------
    # The ONLY account this dashboard reads. Trades are never placed from here.
    agentic_account_masked: str = Field(default="••••4025")

    # --- Paths (inside the container; the data/ + logs/ dirs are volume-mounted) ------------
    data_dir: Path = Field(default=Path("/app/data"))
    logs_dir: Path = Field(default=Path("/app/logs"))

    # --- Live marks (yfinance) --------------------------------------------------------------
    marks_ttl_seconds: int = Field(default=45, ge=5, le=600)

    # --- CORS -------------------------------------------------------------------------------
    # Comma-separated list of explicitly-allowed browser origins. Empty by default: the dashboard's
    # randomly-chosen frontend port is instead matched by ``cors_origin_regex`` below (localhost /
    # 127.0.0.1 on any port). Set this in shared/remote deploys to pin exact origins. We deliberately
    # do NOT default to "*" — this tool is wired to a live brokerage snapshot, a billable API key, and
    # a refresh endpoint with a real side effect, so any arbitrary website must not be able to script
    # it. ``allow_credentials`` stays False, so even the localhost regex carries no cookie/auth risk.
    cors_origins: str = Field(default="")

    # Regex matching the local dev origins (localhost / 127.0.0.1 on any port). This is the default
    # allow rule so the random frontend port works out of the box without wildcarding every origin.
    # Set to empty to disable and rely solely on the explicit ``cors_origins`` list.
    cors_origin_regex: str = Field(default=r"http://(localhost|127\.0\.0\.1):\d+")

    # --- Refresh bridge ---------------------------------------------------------------------
    # Cooldown between honored refresh requests, so a mashed button can't spawn a tab storm.
    refresh_cooldown_seconds: int = Field(default=20, ge=0, le=600)

    # --- Debate rate limit ------------------------------------------------------------------
    # Each debate spends real Anthropic tokens; cap how often one can be kicked off.
    debate_min_interval_seconds: int = Field(default=15, ge=0, le=600)

    # --- Scan limits ------------------------------------------------------------------------
    # Max user-supplied tickers per scan request. Each ticker fans out a blocking yfinance fetch on
    # the threadpool, so an unbounded list is a cheap DoS — cap it (pydantic 422s anything larger).
    scan_max_tickers: int = Field(default=50, ge=1, le=500)

    @property
    def snapshot_path(self) -> Path:
        return self.data_dir / "account_snapshot.json"

    @property
    def refresh_request_path(self) -> Path:
        return self.data_dir / "refresh.request"

    @property
    def debates_dir(self) -> Path:
        return self.logs_dir / "debates"

    @property
    def events_path(self) -> Path:
        return self.logs_dir / "events.jsonl"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def cors_origin_regex_or_none(self) -> str | None:
        regex = self.cors_origin_regex.strip()
        return regex or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton so every dependency sees the same config."""
    return Settings()
