"""FastAPI application entrypoint for the agentic dashboard backend.

Wires CORS, ensures the mounted data/logs directories exist, and mounts the API routers.
Routers are added incrementally as the build progresses; ``health`` is always present.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import account, debate, health, pipeline, refresh, scan

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agentic.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Make sure the volume-mounted dirs exist before any request touches them."""
    settings = get_settings()
    for path in (settings.data_dir, settings.logs_dir, settings.debates_dir):
        path.mkdir(parents=True, exist_ok=True)
    logger.info(
        "backend up | account=%s snapshot_present=%s debate_ready=%s",
        settings.agentic_account_masked,
        settings.snapshot_path.exists(),
        settings.anthropic_api_key is not None,
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Agentic Robinhood Dashboard",
        description="Read-only account monitor + live Sprinkle Sauce screen + jury debate engine.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Default: allow localhost/127.0.0.1 on any port (the frontend port is random) via regex, plus any
    # explicit origins from CORS_ORIGINS. We never default to "*" — this backend fronts a live
    # brokerage snapshot, a billable API key, and a refresh endpoint with a real side effect.
    # allow_credentials stays False, so the permissive localhost regex carries no cookie/auth risk.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex_or_none,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(account.router)
    app.include_router(refresh.router)
    app.include_router(scan.router)
    app.include_router(debate.router)
    app.include_router(pipeline.router)
    return app


app = create_app()
