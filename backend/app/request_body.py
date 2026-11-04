"""Read a request body under a byte cap (BE-13)."""
from __future__ import annotations

from starlette.requests import Request


async def read_capped_body(request: Request, limit: int) -> bytes | None:
    """The body, or None past ``limit`` bytes (never buffered whole).

    Counts what arrives, not the declared ``Content-Length``, so a body
    with no declared length (chunked) stops at the cap too.
    """
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
