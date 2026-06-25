"""Server-Sent Events helpers.

These endpoints are POST (they take a JSON body), so the browser reads them with fetch + a stream
reader rather than EventSource. We still use SSE framing (`data: <json>\\n\\n`) so the client parser
is trivial and identical across the scan, debate, and pipeline streams.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse


def sse(data: dict) -> str:
    """Frame one event as an SSE `data:` line."""
    return f"data: {json.dumps(data)}\n\n"


def sse_response(events: AsyncIterator[dict]) -> StreamingResponse:
    """Wrap an async dict-generator as a streaming SSE response with no proxy buffering."""

    async def body() -> AsyncIterator[str]:
        async for event in events:
            yield sse(event)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
