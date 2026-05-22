"""OpenAI-compatible adapter — validates by GET {base_url}/v1/models."""
from __future__ import annotations

from typing import Optional

import httpx

from app.services.ai_providers.base import (
    AIProviderError,
    LLMResponse,
    ValidateResult,
)


VALIDATE_TIMEOUT_S = 10.0
CHAT_TIMEOUT_S = 30.0
DEFAULT_CAPABILITIES = ["chat", "embed"]


class OpenAICompatibleAdapter:
    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def validate(self) -> ValidateResult:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT_S) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            # NEVER ``str(exc)`` — exception reprs from httpx can include
            # the URL (with embedded creds) or other request context.
            return ValidateResult(
                ok=False, error=f"Network error: {type(exc).__name__}"
            )
        if resp.status_code != 200:
            # Do NOT echo provider response body — a hostile OAI-compatible
            # endpoint can mirror request headers / body back, leaking the
            # plaintext API key that just left this process.
            if 400 <= resp.status_code < 500:
                return ValidateResult(
                    ok=False,
                    error=f"Provider rejected the request ({resp.status_code})",
                )
            return ValidateResult(
                ok=False,
                error=f"Provider unavailable ({resp.status_code})",
            )
        try:
            payload = resp.json()
        except ValueError:
            return ValidateResult(ok=False, error="Provider returned invalid JSON")
        models = [
            m["id"]
            for m in payload.get("data", [])
            if isinstance(m, dict) and "id" in m
        ]
        return ValidateResult(
            ok=True,
            discovered_models=models,
            discovered_capabilities=list(DEFAULT_CAPABILITIES),
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """POST {base_url}/v1/chat/completions.

        Same response shape as the OpenAI adapter — hostile OAI-compatible
        endpoints can still mirror the request body back, so error
        wrapping never echoes provider content.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {"model": model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(url, headers=headers, json=body)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise AIProviderError(
                code=f"provider_status_{resp.status_code}",
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except ValueError:
            raise AIProviderError(code="provider_invalid_json") from None
        try:
            content = payload["choices"][0]["message"]["content"] or ""
            usage = payload.get("usage", {}) or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        except (KeyError, IndexError, TypeError):
            raise AIProviderError(code="provider_unexpected_shape") from None
        return LLMResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=payload.get("model", model) or model,
        )

    async def chat_structured(self, *, model, messages, schema, max_tokens=None):
        raise NotImplementedError("PR3")
