"""GET/PUT /api/settings — the tunable parameters, their bounds, and who changed them.

The registry in services/settings_store.py is the single authority; this router is a thin edge over
it. Bounds live there rather than here so the API, the settings page and the consumers cannot
disagree about what is allowed.

WRITES ARE ATTRIBUTED
    A threshold is a claim about how the book should behave. `updated_by` is the operator's email
    taken from the SESSION, never from the request body — a client-supplied actor is an unsigned
    claim about who did something, which is worse than no attribution at all.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import DbUnavailable
from app.services import settings_store as store

logger = logging.getLogger("agentic.api.settings")

router = APIRouter(prefix="/api", tags=["settings"])


def _catalogue(values: dict[str, float]) -> list[dict[str, Any]]:
    return [
        {
            "key": p.key,
            "label": p.label,
            "group": p.group,
            "unit": p.unit,
            "value": values[p.key],
            "default": p.default,
            "min": p.minimum,
            "max": p.maximum,
            "help": p.help,
            "used_by": p.used_by,
            "is_default": values[p.key] == p.default,
        }
        for p in store.REGISTRY
    ]


@router.get("/settings")
def get_settings_catalogue() -> dict[str, Any]:
    values, source = store.get_all()
    try:
        changes = store.history(limit=25)
    except DbUnavailable:
        changes = []
    return {
        "meta": {
            # 'defaults' means the database could not be read and every value below is the compiled
            # default — NOT that the operator chose them. The page must be able to say which.
            "source": source,
            "count": len(store.REGISTRY),
            # Read-only here on purpose: docs/SLATE.md is the authority for these, because the
            # document an owner edits is meant to outrank the dashboard.
            "document_sourced": [
                {"label": "Hard stop", "value_note": "parsed from docs/SLATE.md §Sizing discipline"},
                {"label": "Trim multiple", "value_note": "parsed from docs/SLATE.md §Sizing discipline"},
            ],
        },
        "parameters": _catalogue(values),
        "history": changes,
    }


class SettingUpdate(BaseModel):
    value: float = Field(description="the new value, in the parameter's own unit")


@router.put("/settings/{key}")
def update_setting(key: str, req: SettingUpdate, request: Request) -> dict[str, Any]:
    actor = None
    operator = getattr(request.state, "operator", None)
    if operator is not None:
        actor = getattr(operator, "email", None) or str(operator)

    try:
        stored = store.set_value(key, req.value, actor=actor)
    except store.SettingError as exc:
        # 422, and the message is the registry's own sentence naming the bound. A generic
        # "invalid value" would leave the operator guessing at a number they cannot see.
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except DbUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The database is unavailable, so the change was not saved: {exc}",
        ) from None

    logger.warning("setting changed: %s = %s by %s", key, stored, actor or "unknown")
    values, source = store.get_all()
    return {"key": key, "value": stored, "source": source, "parameters": _catalogue(values)}
