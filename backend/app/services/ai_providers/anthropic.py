"""Anthropic adapter — validate via GET /v1/models, chat via /v1/messages."""
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
DEFAULT_CAPABILITIES = ["chat"]
MODELS_URL = "https://api.anthropic.com/v1/models"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Anthropic requires max_tokens on every messages call.
DEFAULT_CHAT_MAX_TOKENS = 1024


class AnthropicAdapter:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    async def validate(self) -> ValidateResult:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        try:
            async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT_S) as client:
                resp = await client.get(MODELS_URL, headers=headers)
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
        """POST /v1/messages. Maps response.content[*].text -> content.

        Anthropic separates ``system`` from the user/assistant turns.
        We do the standard remap: any ``role == "system"`` message
        becomes the top-level ``system`` field; the rest stay in
        ``messages``.
        """
        system_parts: list[str] = []
        chat_messages: list[dict] = []
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content") or ""
                if content:
                    system_parts.append(str(content))
            else:
                chat_messages.append(m)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens or DEFAULT_CHAT_MAX_TOKENS,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)

        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(
                    MESSAGES_URL, headers=headers, json=body
                )
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
            blocks = payload.get("content", []) or []
            content_text = "".join(
                str(b.get("text", "") or "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            usage = payload.get("usage", {}) or {}
            prompt_tokens = int(usage.get("input_tokens", 0) or 0)
            completion_tokens = int(usage.get("output_tokens", 0) or 0)
        except (KeyError, TypeError):
            raise AIProviderError(code="provider_unexpected_shape") from None
        return LLMResponse(
            content=content_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=payload.get("model", model) or model,
        )

    async def chat_structured(self, *, model, messages, schema, max_tokens=None):
        raise NotImplementedError("PR3")
