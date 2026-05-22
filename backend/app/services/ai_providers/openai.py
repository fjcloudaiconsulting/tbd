"""OpenAI adapter — validate via GET /v1/models, chat via /v1/chat/completions.

PR3 adds the remaining capabilities: ``embed`` (text-embedding-3-small
default), ``chat_structured`` (JSON-mode), ``function_call`` (tool use),
``stream`` (SSE).
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
# Baseline capabilities advertised for any healthy OpenAI key. The
# ``structured_output`` capability is gated to the json_schema-capable
# model subset and added on top of the baseline in ``validate()``.
BASELINE_CAPABILITIES = ["chat", "embed", "function_call", "stream"]
DEFAULT_EMBED_MODEL = "text-embedding-3-small"
MODELS_URL = "https://api.openai.com/v1/models"
CHAT_URL = "https://api.openai.com/v1/chat/completions"
EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# Models that support the typed json_schema response_format (post 2024-08).
# Older models fall through to plain json_object mode with the schema in
# the system prompt.
JSON_SCHEMA_MODELS = (
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4o-2024-08-06",
    "gpt-4.1",
    "gpt-5",
)


class OpenAIAdapter:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    async def validate(self) -> ValidateResult:
        headers = {"Authorization": f"Bearer {self.api_key}"}
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
        capabilities = list(BASELINE_CAPABILITIES)
        # ``structured_output`` is gated to the json_schema-capable
        # subset (gpt-4o family, gpt-4.1, gpt-5). If the key has access
        # to any of those, advertise the capability.
        if any(
            m.startswith(prefix) for m in models for prefix in JSON_SCHEMA_MODELS
        ):
            capabilities.append("structured_output")
        return ValidateResult(
            ok=True,
            discovered_models=models,
            discovered_capabilities=capabilities,
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """POST /v1/chat/completions. 30s timeout.

        Errors are wrapped in ``AIProviderError`` with a sanitized
        ``code`` — never the provider's response body, never the raw
        exception repr (same posture as ``validate``).
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {"model": model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(CHAT_URL, headers=headers, json=body)
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
        """POST /v1/embeddings.

        Defaults to ``text-embedding-3-small``. Same sanitized error
        wrapping as ``chat``.
        """
        actual_model = model or DEFAULT_EMBED_MODEL
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": actual_model, "input": texts}
        try:
            async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as client:
                resp = await client.post(
                    EMBEDDINGS_URL, headers=headers, json=body
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
            model=payload.get("model", actual_model) or actual_model,
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
        """JSON-mode chat call. Schema-validation + retry budget live
        in the SERVICE layer (``call_llm_structured``).

        Newer models (gpt-4o family) use the typed ``json_schema``
        response_format. Older models fall back to plain ``json_object``
        with the schema injected into the system message. Choice is
        based on model name prefix.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        use_json_schema = any(model.startswith(p) for p in JSON_SCHEMA_MODELS)
        body: dict = {"model": model, "messages": list(messages)}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if use_json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
            # Older models need the schema described in the messages.
            schema_hint = (
                "Output ONLY a JSON object matching this schema: "
                + json.dumps(schema, sort_keys=True)
            )
            body["messages"] = [{"role": "system", "content": schema_hint}] + list(
                messages
            )
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(CHAT_URL, headers=headers, json=body)
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
        """POST /v1/chat/completions with OpenAI tools array.

        ``tools`` is the canonical OpenAI tool-calling shape:
        ``[{"type": "function", "function": {"name": ..., "parameters": {...}}}]``.
        Returns ``FunctionCallResponse.tool_calls`` with parsed arguments.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {"model": model, "messages": messages, "tools": tools}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                resp = await client.post(CHAT_URL, headers=headers, json=body)
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
        """POST /v1/chat/completions with ``stream=true``.

        Yields ``StreamChunk(delta_text=..., done=False)`` for each
        incremental chunk; emits a final ``StreamChunk(done=True,
        final_usage=...)`` after the SSE ``[DONE]`` sentinel. Token
        usage comes from the optional ``stream_options.include_usage``
        block when the provider sends one; otherwise the service
        layer fills in an estimate from the accumulated text length.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        final_usage: Optional[TokenUsage] = None
        try:
            async with httpx.AsyncClient(timeout=STREAM_TIMEOUT_S) as client:
                async with client.stream(
                    "POST", CHAT_URL, headers=headers, json=body
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
                            # malformed line — skip; per-line resilience
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
