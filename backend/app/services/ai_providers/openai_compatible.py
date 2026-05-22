"""OpenAI-compatible adapter — validates by GET {base_url}/v1/models.

Same wire shape as the OpenAI adapter; the only difference is the
URL prefix. PR3 mirrors the same capability surface — embed,
chat_structured, function_call, stream — but with hostile-endpoint
sanitization on every error path (these endpoints can mirror request
headers back, which would leak the plaintext API key that just left
this process).

Capability advertising: this adapter implements all 5 capabilities
(chat, embed, structured_output, function_call, stream) against the
OpenAI HTTP shape. The validate() probe advertises them all because
the user explicitly opted into trusting their configured endpoint by
typing in its URL. If the underlying server doesn't honor a request,
the sanitized error path returns a generic ``Provider rejected the
request (status)`` envelope rather than leaking provider response
data — see the PR1 sanitization contract.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from app.services.ai_providers.base import (
    AIProviderError,
    EmbedResponse,
    FunctionCallResponse,
    LLMResponse,
    StreamChunk,
    TokenUsage,
    ValidateResult,
)


VALIDATE_TIMEOUT_S = 10.0
CHAT_TIMEOUT_S = 30.0
EMBED_TIMEOUT_S = 30.0
STREAM_TIMEOUT_S = 60.0
# OpenAI-compatible endpoints (vLLM, llama.cpp, LM Studio, third-party
# hosts) cannot be introspected for individual capability support — the
# /v1/models response is just a list of model IDs with no capability
# metadata. We advertise the full OpenAI HTTP capability surface that
# this adapter actually implements (chat, embed, structured_output,
# function_call, stream). The user opted into trusting the endpoint
# when they typed in its URL; if the underlying server doesn't honor a
# request, the sanitized error path returns a generic ``Provider
# rejected the request (status)`` envelope without leaking provider
# response data (PR1 sanitization contract).
DEFAULT_CAPABILITIES = [
    "chat",
    "embed",
    "structured_output",
    "function_call",
    "stream",
]


class OpenAICompatibleAdapter:
    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def validate(self) -> ValidateResult:
        """GET /v1/models and advertise the full capability surface.

        OpenAI-compatible servers can't be introspected for individual
        capability support, so we advertise everything this adapter
        implements (chat, embed, structured_output, function_call,
        stream) and trust the user-configured endpoint. If the
        underlying server doesn't honor a particular capability, the
        sanitized error path (PR1 contract) surfaces a generic
        ``Provider rejected the request (status)`` envelope without
        leaking provider response data into the error.
        """
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
        """POST {base_url}/v1/chat/completions."""
        body: dict = {"model": model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=body
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

    async def embed(
        self,
        *,
        texts: list[str],
        model: Optional[str] = None,
    ) -> EmbedResponse:
        """POST {base_url}/v1/embeddings.

        The model parameter is required — OpenAI-compatible servers
        don't share OpenAI's default model name.
        """
        if not model:
            raise AIProviderError(code="oai_compatible_embed_model_required")
        body = {"model": model, "input": texts}
        url = f"{self.base_url}/v1/embeddings"
        try:
            async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=body
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
            data = payload.get("data", []) or []
            vectors = [
                [float(x) for x in (row.get("embedding") or [])]
                for row in data
                if isinstance(row, dict)
            ]
            usage = payload.get("usage", {}) or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        except (KeyError, TypeError, ValueError):
            raise AIProviderError(code="provider_unexpected_shape") from None
        return EmbedResponse(
            vectors=vectors,
            model=payload.get("model", model) or model,
            prompt_tokens=prompt_tokens,
        )

    async def chat_structured(
        self,
        *,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """OpenAI-compatible JSON-mode chat.

        Conservatively uses plain ``response_format={"type": "json_object"}``
        with the schema injected into the system prompt — the typed
        ``json_schema`` mode is OpenAI-specific and may not be honored
        by every compatible server.
        """
        schema_hint = (
            "Output ONLY a JSON object matching this schema: "
            + json.dumps(schema, sort_keys=True)
        )
        body: dict = {
            "model": model,
            "messages": [{"role": "system", "content": schema_hint}]
            + list(messages),
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=body
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

    async def function_call(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: Optional[int] = None,
    ) -> FunctionCallResponse:
        """POST {base_url}/v1/chat/completions with tools.

        If the actual server doesn't support function calling, the
        upstream 4xx error will bubble through as a sanitized
        ``AIProviderError`` — we let the caller decide rather than
        block on a static allowlist (the OAI-compatible space is too
        broad).
        """
        body: dict = {"model": model, "messages": messages, "tools": tools}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=body
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
            message = payload["choices"][0]["message"]
            raw_tool_calls = message.get("tool_calls") or []
            tool_calls: list[dict] = []
            for call in raw_tool_calls:
                fn = call.get("function") or {}
                args_text = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(args_text)
                except (TypeError, ValueError):
                    parsed_args = {}
                tool_calls.append(
                    {
                        "name": fn.get("name") or "",
                        "arguments": parsed_args,
                    }
                )
            content = message.get("content") or ""
            usage = payload.get("usage", {}) or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        except (KeyError, IndexError, TypeError):
            raise AIProviderError(code="provider_unexpected_shape") from None
        return FunctionCallResponse(
            tool_calls=tool_calls,
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=payload.get("model", model) or model,
        )

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamChunk]:
        """POST {base_url}/v1/chat/completions with ``stream=true``.

        Mirrors the OpenAI streaming shape. ``include_usage`` is
        best-effort — compatible servers vary on whether they emit the
        final usage block.
        """
        headers = {**self._headers(), "Accept": "text/event-stream"}
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        url = f"{self.base_url}/v1/chat/completions"

        final_usage: Optional[TokenUsage] = None
        try:
            async with httpx.AsyncClient(timeout=STREAM_TIMEOUT_S) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=body
                ) as resp:
                    if resp.status_code != 200:
                        raise AIProviderError(
                            code=f"provider_status_{resp.status_code}",
                            status_code=resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except ValueError:
                            continue
                        choices = event.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            delta_text = str(delta.get("content") or "")
                            if delta_text:
                                yield StreamChunk(
                                    delta_text=delta_text, done=False
                                )
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            final_usage = TokenUsage(
                                prompt_tokens=int(
                                    usage.get("prompt_tokens", 0) or 0
                                ),
                                completion_tokens=int(
                                    usage.get("completion_tokens", 0) or 0
                                ),
                            )
        except AIProviderError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        yield StreamChunk(delta_text="", done=True, final_usage=final_usage)
