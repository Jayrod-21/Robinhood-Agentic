"""GET /api/accounts — which brokerage accounts this deployment can read.

Populates the switcher with NAMES rather than numbers: "AI Agentic Debate" and "ML testing" are
distinguishable at a glance in a way that "1" and "4" are not, and the whole risk of a multi-account
dashboard is looking at the wrong book without noticing.

NO CREDENTIAL MATERIAL, EVER
    id, name, and whether the endpoint is paper. Not the key id — that is half a credential, and a
    dashboard that displays it teaches an operator it is safe to paste somewhere.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services import accounts

router = APIRouter(prefix="/api", tags=["accounts"])


@router.get("/accounts")
def list_accounts() -> dict[str, Any]:
    configured = accounts.profiles()
    return {
        "meta": {
            "count": len(configured),
            "default_account_id": accounts.DEFAULT_ACCOUNT_ID,
            # True when every configured account points at a paper endpoint. The switcher can use
            # this to decide whether the live/paper distinction needs to be on screen at all — and
            # it going false is a fact an operator must never learn by accident.
            "all_paper": all(p.is_paper for p in configured) if configured else True,
        },
        "accounts": [p.describe() for p in configured],
    }
