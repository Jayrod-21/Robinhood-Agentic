"""Runtime configuration, loaded from the environment (and ``backend/.env`` for local dev).

Every tunable lives here so the rest of the app never reaches into ``os.environ`` directly — with
one deliberate exception: the Alpaca credential pair (``ALPACA_API_KEY_ID`` /
``ALPACA_API_SECRET_KEY``) is checked straight from the environment by ``services/broker.py``. The
Anthropic key is the one secret loaded here: it is read here, used only by the debate engine, and is
never serialized into any API response or exposed to the frontend.
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

    # --- Account / Robinhood (fallback snapshot path) ---------------------------------------
    # Masked number of the Robinhood account behind the fallback snapshot file and the refresh
    # bridge. When Alpaca credentials are configured, services/broker.py serves the Alpaca account
    # of record instead and this identifies only the fallback/refresh path. Trades are never
    # placed from here either way.
    agentic_account_masked: str = Field(default="••••4025")

    # --- Paths (inside the container; the data/ + logs/ dirs are volume-mounted) ------------
    data_dir: Path = Field(default=Path("/app/data"))
    # SLATE.md and THESES.md. Volume-mounted at /app/docs in both stacks; on a developer host the
    # repo copy is used instead.
    #
    # Resolved by LOOKING rather than by counting directories up from __file__. The container path
    # (/app/app/routers/x.py) and the host path (backend/app/routers/x.py) sit at different depths
    # relative to docs/, so any single parents[N] is wrong in one of the two environments — which
    # is how the first version resolved to "/docs" in prod and 503'd the reconciliation page while
    # every host test passed.
    docs_dir_override: Path | None = Field(default=None)
    logs_dir: Path = Field(default=Path("/app/logs"))

    # --- Live marks (FMP) -------------------------------------------------------------------
    # 120s = the 2-minute position refresh the operator chose. Sized against measurement rather
    # than taste: a quote is ~455 bytes, so fifteen positions refreshed every two minutes for a
    # whole trading day is ~3,600 calls and 1.6 MB — 0.03 GB over 30 sessions, against a 20 GB
    # allowance. Bandwidth is nowhere near the binding constraint; call pacing is.
    #
    # This is the FALLBACK. The live value comes from settings (marks_ttl_seconds) so the cadence
    # can be tuned without a redeploy.
    marks_ttl_seconds: int = Field(default=120, ge=5, le=600)

    # How old the ACCOUNT data may be before the Data-Trust strip calls it stale.
    #
    # There was no such rule until now, and its absence is why a snapshot dated 27 July rendered
    # for three weeks without a word: the account view tracked whether PRICES were live and never
    # asked whether the HOLDINGS were. Price freshness and position freshness are different
    # questions, and only one of them was being answered.
    #
    # 10 minutes suits both sources. A live broker read is seconds old, so this never fires for
    # Alpaca unless something is genuinely wrong; the fallback snapshot file is written by a
    # human-triggered refresh, so anything older than a coffee break is worth flagging.
    snapshot_max_age_seconds: int = Field(default=600, ge=30, le=86_400)

    # --- Execution (docs/EXECUTION_DESIGN.md) -------------------------------------------------
    # THE OUTER SWITCH. False means the order endpoints answer 403 and arming is impossible — not
    # "orders are unlikely", but "there is no path". This is what ships, and turning it on is a
    # deliberate act by someone who has read the design doc.
    execution_enabled: bool = Field(default=False)

    # Live trading is a SECOND, separate switch. execution_enabled alone only ever reaches the paper
    # endpoint: every execution path calls assert_paper() unless this is true. Two switches because
    # one switch means the day you enable execution you also enable it against real money, and those
    # are different decisions made on different days.
    execution_allow_live: bool = Field(default=False)

    # Order types permitted, comma-separated. Defaults to limit-only (design §6.2). Limit is a strict
    # SUBSET of limit+market, so widening this later is additive; shipping permissive and narrowing
    # later breaks whatever came to depend on it. A market order also hands the price decision to the
    # book, and the preview an owner approved is the whole control in this design.
    execution_order_types: str = Field(default="limit")

    # How long an arming window lasts. Confirmation is still required per order; the window only
    # spares the operator from re-arming between them.
    execution_arm_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    # A preview older than this cannot be confirmed. It is sized against account state and prices
    # fetched at preview time; confirming a stale one would submit an order the operator approved
    # against numbers that no longer hold. Refused, never silently re-priced.
    execution_preview_ttl_seconds: int = Field(default=120, ge=15, le=900)

    # Hard cap on submission ATTEMPTS per window, enforced server-side and independent of any client.
    # Counts attempts rather than successes: a loop failing validation still burns the budget, which
    # is the point — the cap exists to stop a runaway, and a runaway that fails fast is still a
    # runaway. Tripping it DISARMS execution and requires a human to re-arm.
    execution_max_orders_per_window: int = Field(default=10, ge=1, le=100)
    execution_rate_window_seconds: int = Field(default=3600, ge=60, le=86_400)

    # --- Guardrail thresholds (charter §5, docs/AGENTIC_ROBINHOOD_v1.md) ----------------------
    # Config, not constants: the charter's numbers are tuned for a $100 aggressive account and will
    # not survive contact with a different account size. Tunable and observable — every evaluation
    # writes a guardrail_events row naming the threshold it used, so a blocked order can always be
    # traced to the number that blocked it rather than to "the system said no".
    guardrail_cash_floor_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    guardrail_max_position_pct: float = Field(default=25.0, ge=1.0, le=100.0)
    guardrail_max_names: int = Field(default=6, ge=1, le=50)
    guardrail_max_drawdown_pct: float = Field(default=25.0, ge=1.0, le=100.0)

    @property
    def execution_order_type_list(self) -> list[str]:
        """The permitted order types, normalised. Empty means execution is impossible, not open."""
        return [t.strip().lower() for t in self.execution_order_types.split(",") if t.strip()]

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

    # --- Transactional email (auth verification + security notices) ------------------------
    # SMTP relay settings, mirroring 9b Korean Master's names (SMTP_HOST/PORT/USER/PASS/FROM,
    # SMTP_TLS_REJECT_UNAUTHORIZED). In this deployment the env points at Proton Mail Bridge on the
    # docker host (host.docker.internal:1025 via the compose extra_hosts entry), but nothing in the
    # mail service knows that — any RFC-compliant relay works.
    #
    # SMTP_HOST unset (or empty) selects the log-only mock transport in app/services/email.py:
    # dev and CI never open a socket and never send real mail. That is a deliberate mirror of 9b's
    # behavior, not a fallback-by-accident; see AUTH_THREAT_MODEL.md §5.6 for the prod-profile guard.
    smtp_host: str | None = Field(default=None)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str | None = Field(default=None)
    smtp_pass: str | None = Field(default=None)
    # Verified sender (live self-send confirmed against Proton). Override only if the sender
    # identity actually changes — Proton rejects From addresses the account does not own.
    smtp_from: str = Field(default="ww.notifications@jaredstudio.com")
    # TLS certificate verification for the STARTTLS hop. Default ON. The only sanctioned reason to
    # set this false is Proton Mail Bridge presenting a self-signed certificate on a loopback /
    # host-gateway hop (the same reason 9b has this exact flag). The session is still encrypted;
    # only chain validation is skipped, and only when the operator explicitly opts out in env.
    smtp_tls_reject_unauthorized: bool = Field(default=True)
    # Bound every SMTP socket operation so a dead relay fails fast instead of pinning a worker
    # thread (the async callers time out with it).
    smtp_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    # Public origin of the dashboard, used to build absolute links in emails (the verification link
    # target). Prod sets the real origin (e.g. https://ww.jaredstudio.com); the default serves local
    # dev where the mock transport logs the link instead of sending it.
    public_origin: str = Field(default="http://localhost:3000")

    @field_validator("smtp_host", "smtp_user", "smtp_pass", mode="before")
    @classmethod
    def _empty_smtp_value_is_none(cls, v: object) -> object:
        # Same reasoning as _empty_key_is_none: compose-style ${VAR:-} interpolation passes "" for
        # unset vars, and "" must mean absent so the mock-transport selection can't be fooled into
        # dialing an empty hostname.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # --- Authentication (issue #17, docs/AUTH_THREAT_MODEL.md) ------------------------------
    # Challenge tokens are the password step's only product (§4): single-use, purpose-scoped,
    # and short-lived — they confer nothing but the right to attempt the TOTP step.
    auth_challenge_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    # Session lifetime (§5.3): absolute expiry (default 14 days) and idle timeout (default 24h),
    # BOTH enforced server-side on every request — cookie Max-Age is client-controlled and only
    # ever a hint. NOTE: the TOTP acceptance window is deliberately NOT here — §5.4 pins it as a
    # code constant (services/auth.py::TOTP_STEP_WINDOW) so widening it requires a review.
    auth_session_ttl_hours: int = Field(default=336, ge=1, le=2160)
    auth_session_idle_hours: int = Field(default=24, ge=1, le=336)
    # §5.8 lockout: tunable config, not constants, per the guardrail rules (a guardrail that
    # blocks a valid operator must be adjustable and observable — bin/manage_operator.py unlock
    # is the immediate override). Failures count at the TOTP step ONLY, which is reachable only
    # after a correct password, so spraying an email cannot lock an operator out.
    totp_max_failed_attempts: int = Field(default=5, ge=1, le=20)
    totp_lockout_minutes: int = Field(default=15, ge=1, le=1440)
    # §5.6: verification-token TTL and the per-operator resend cooldown (enforced atomically
    # with the token insert; suppression is invisible in the response).
    auth_verification_ttl_hours: int = Field(default=24, ge=1, le=168)
    auth_resend_cooldown_seconds: int = Field(default=60, ge=0, le=3600)

    # --- Scan limits ------------------------------------------------------------------------
    # Max user-supplied tickers per scan request. Each ticker fans out a blocking FMP bundle on
    # the threadpool, so an unbounded list is a cheap DoS — cap it (pydantic 422s anything larger).
    scan_max_tickers: int = Field(default=50, ge=1, le=500)

    @property
    def docs_dir(self) -> Path:
        if self.docs_dir_override is not None:
            return self.docs_dir_override
        container = Path("/app/docs")
        if container.is_dir():
            return container
        # backend/app/config.py -> backend/app -> backend -> <repo>
        return Path(__file__).resolve().parents[2] / "docs"

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
