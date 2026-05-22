"""Ollama adapter — validates by GET {base_url}/api/tags.

Auth on Ollama is rare in the wild but supported via an optional
``Bearer`` token when fronting the server with a reverse proxy.
PR2 adds the ``chat()`` pass-through against ``/api/chat``.
"""
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


class OllamaAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bearer_token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # ``api_key`` is required by the create form but Ollama itself
        # ignores it. Stored anyway so a rotation flow can sit on the
        # same shape as the other adapters.
        self.api_key = api_key
        self.bearer_token = bearer_token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    async def validate(self) -> ValidateResult:
        headers = self._headers()
        url = f"{self.base_url}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT_S) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return ValidateResult(
                ok=False, error=f"Network error: {type(exc).__name__}"
            )
        if resp.status_code != 200:
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
            m["name"]
            for m in payload.get("models", [])
            if isinstance(m, dict) and "name" in m
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
        """POST {base_url}/api/chat. ``stream=false`` so we get one
        complete response back, not an NDJSON stream.

        Ollama doesn't return a stable token-count contract — newer
        builds emit ``prompt_eval_count`` / ``eval_count`` at the top
        level, older ones don't. We use them when present and fall
        back to 0 (cost falls back to the ``_default`` pricing row,
        which is conservatively high — see ``ai_pricing``).
        """
        headers = {**self._headers(), "Content-Type": "application/json"}
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if max_tokens is not None:
            body["options"] = {"num_predict": max_tokens}
        url = f"{self.base_url}/api/chat"
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
            message = payload.get("message", {}) or {}
            content = str(message.get("content", "") or "")
            prompt_tokens = int(payload.get("prompt_eval_count", 0) or 0)
            completion_tokens = int(payload.get("eval_count", 0) or 0)
        except (KeyError, TypeError):
            raise AIProviderError(code="provider_unexpected_shape") from None
        return LLMResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=payload.get("model", model) or model,
        )

    async def chat_structured(self, *, model, messages, schema, max_tokens=None):
        raise NotImplementedError("PR3")
