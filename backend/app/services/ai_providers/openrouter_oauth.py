"""OpenRouter OAuth PKCE code exchange (TBD-632).

``POST /api/v1/auth/keys`` swaps the one-time ``code`` plus the PKCE verifier for
an API key. Anything but a clear 4xx refusal or a well-formed 200 may have left
a live key at OpenRouter, so those raise :class:`ExchangeFailed` (the caller
treats it as a possible orphan). Never log the code, verifier, key or body.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from app.schemas.org_ai_credential import API_KEY_MAX_LENGTH, API_KEY_MIN_LENGTH
from app.services.ai_providers.egress_guard import guarded_async_client
from app.services.ai_providers.openai_compatible import OPENROUTER_BASE_URL

EXCHANGE_URL = f"{OPENROUTER_BASE_URL}/auth/keys"
DEADLINE_SECONDS = 15
MAX_BODY_BYTES = 65_536

# Tests inject a transport so nothing depends on DNS.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class CodeRejected(Exception):
    """OpenRouter clearly refused; no key was minted. ``busy`` is a 429."""

    def __init__(self, busy: bool):
        super().__init__("rejected")
        self.busy = busy


class ExchangeFailed(Exception):
    """Any other outcome after the request was sent; a key may exist."""


async def exchange_code(code: str, verifier: str) -> str:
    client = (
        httpx.AsyncClient(timeout=10, transport=TRANSPORT, follow_redirects=False, trust_env=False)
        if TRANSPORT is not None
        else guarded_async_client(timeout=10)
    )
    body = {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
    async with asyncio.timeout(DEADLINE_SECONDS):
        async with client:
            # identity encoding: the byte cap must bound decompressed bytes
            async with client.stream(
                "POST", EXCHANGE_URL, json=body, headers={"Accept-Encoding": "identity"}
            ) as r:
                if r.status_code == 429:
                    raise CodeRejected(busy=True)
                # OpenRouter documents only 400 and 403; anything else may have minted a key.
                if r.status_code in (400, 403):
                    raise CodeRejected(busy=False)
                if r.status_code != 200:
                    raise ExchangeFailed("status")
                if r.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ExchangeFailed("encoding")
                raw = b""
                async for chunk in r.aiter_raw():
                    raw += chunk
                    if len(raw) > MAX_BODY_BYTES:
                        raise ExchangeFailed("body too large")
    try:
        key = json.loads(raw).get("key")
    except (ValueError, AttributeError):
        raise ExchangeFailed("body") from None
    if (
        not isinstance(key, str)
        or len(key.strip()) < API_KEY_MIN_LENGTH
        or len(key) > API_KEY_MAX_LENGTH
    ):
        raise ExchangeFailed("key")
    return key
