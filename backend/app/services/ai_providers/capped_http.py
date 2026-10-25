"""Byte-capped reads of AI provider responses (BE-09).

``client.get`` / ``client.post`` buffer the whole body, and httpx
transparently decodes gzip, deflate, brotli and zstd, so a provider can
answer with an arbitrarily large or highly compressed body. Org admins
choose the ``base_url`` of openai_compatible credentials, so the
far end may be hostile. Every adapter request goes through this module
instead (same pattern as ``openrouter_oauth.exchange_code``):

- ``Accept-Encoding: identity`` is sent and any other ``Content-Encoding``
  on a 200 is refused, so the cap counts the bytes that get parsed;
- the raw body is counted while it arrives and the read stops at the cap;
- streamed responses are split into lines here, with a per-line and a
  whole-stream bound (``httpx.Response.aiter_lines`` has neither).

The reader only consumes what the client returns; the egress guard's
resolve / validate / pin transport is unchanged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from app.services.ai_providers.base import AIProviderError

# A 200 body is parsed whole: model lists, chat completions, embeddings.
MAX_BODY_BYTES = 8 * 1024 * 1024
# A non-200 body only feeds ``provider_reason``; past this it is dropped.
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_STREAM_LINE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024

RESPONSE_TOO_LARGE = "provider_response_too_large"
UNEXPECTED_ENCODING = "provider_unexpected_encoding"

# validate() surfaces a sentence to the admin, not a code.
_VALIDATION_ERRORS = {
    RESPONSE_TOO_LARGE: "Provider response too large",
    UNEXPECTED_ENCODING: "Provider response used an unsupported encoding",
}

# Same line breaks httpx's ``aiter_lines`` honours.
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


@dataclass(frozen=True)
class CappedResponse:
    """Status and fully read (capped) body of a non-stream request."""

    status_code: int
    content: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except RecursionError:
            # Deep nesting is malformed input like any other, not a 500.
            raise ValueError("JSON nested too deeply") from None


def identity_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """``headers`` plus ``Accept-Encoding: identity``."""
    return {**(headers or {}), "Accept-Encoding": "identity"}


def validation_error(exc: AIProviderError) -> str:
    return _VALIDATION_ERRORS.get(exc.code, "Provider returned an unusable response")


def _is_identity(resp: httpx.Response) -> bool:
    encoding = resp.headers.get("content-encoding", "identity").strip().lower()
    return encoding in ("", "identity")


def require_identity(resp: httpx.Response) -> None:
    """Refuse a compressed body. Once this passes, httpx's decoder for the
    response is the identity one, so ``aiter_bytes`` yields the raw bytes."""
    if not _is_identity(resp):
        raise AIProviderError(code=UNEXPECTED_ENCODING)


async def _read(resp: httpx.Response, limit: int) -> bytes | None:
    """The raw body, or None as soon as it passes ``limit`` bytes."""
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    **kwargs: Any,
) -> CappedResponse:
    """Send one request and read its body under the cap.

    A 200 over ``max_bytes`` (default ``MAX_BODY_BYTES``) or with a
    non-identity encoding raises ``AIProviderError``. Any other status
    returns with at most ``MAX_ERROR_BODY_BYTES`` of body, or none.
    """
    limit = MAX_BODY_BYTES if max_bytes is None else max_bytes
    async with client.stream(
        method, url, headers=identity_headers(headers), **kwargs
    ) as resp:
        if resp.status_code != 200:
            body = (
                await _read(resp, MAX_ERROR_BODY_BYTES)
                if _is_identity(resp)
                else None
            )
            return CappedResponse(resp.status_code, body or b"")
        require_identity(resp)
        body = await _read(resp, limit)
        if body is None:
            raise AIProviderError(code=RESPONSE_TOO_LARGE)
        return CappedResponse(resp.status_code, body)


async def aiter_lines(
    resp: httpx.Response,
    *,
    max_line_bytes: int | None = None,
    max_bytes: int | None = None,
) -> AsyncIterator[str]:
    """Lines of a streamed 200, each at most ``MAX_STREAM_LINE_BYTES`` and
    all together at most ``MAX_STREAM_BYTES``; past either it raises
    ``AIProviderError``. A line break split across chunks can yield an
    extra empty line, which every caller already skips."""
    line_limit = MAX_STREAM_LINE_BYTES if max_line_bytes is None else max_line_bytes
    total_limit = MAX_STREAM_BYTES if max_bytes is None else max_bytes
    require_identity(resp)
    total = 0
    pending = bytearray()
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > total_limit:
            raise AIProviderError(code=RESPONSE_TOO_LARGE)
        for i, part in enumerate(_LINE_BREAK.split(chunk)):
            if i:
                yield pending.decode("utf-8", errors="replace")
                pending = bytearray()
            pending += part
            if len(pending) > line_limit:
                raise AIProviderError(code=RESPONSE_TOO_LARGE)
    if pending:
        yield pending.decode("utf-8", errors="replace")
