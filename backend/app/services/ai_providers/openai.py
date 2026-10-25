"""OpenAI adapter — validate via GET /v1/models, chat via /v1/chat/completions.

PR3 adds the remaining capabilities: ``embed`` (text-embedding-3-small
default), ``chat_structured`` (JSON-mode), ``function_call`` (tool use),
``stream`` (SSE).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

import httpx

from app.services.ai_providers import capped_http
from app.services.ai_providers.base import (
    AIProviderError,
    EmbedResponse,
    FunctionCallResponse,
    LLMResponse,
    StreamChunk,
    TokenUsage,
    ValidateResult,
    UNEXPECTED_MODELS_RESPONSE,
    UNEXPECTED_SHAPE_ERRORS,
    embedding_vectors,
    model_ids,
    openai_message_content,
    parse_openai_stream_event,
    provider_reason,
    parse_openai_tool_calls,
    response_model,
    to_openai_messages,
    token_count,
    usage_of,
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
                # ``TimeoutError`` is caught alongside httpx's own timeout
                # classes on every non-stream network site in this file (TBD-329):
                # httpx applies ``timeout=`` PER PHASE, so a drip-feed
                # response can stay "alive" forever; ``asyncio.timeout``
                # supplies the missing aggregate bound and raises a BARE
                # builtin ``TimeoutError``, which derives from neither
                # ``httpx.HTTPError`` nor ``httpx.TimeoutException`` (same
                # finding as ``captcha.py``, TBD-328).
                async with asyncio.timeout(VALIDATE_TIMEOUT_S):
                    resp = await capped_http.request(
                        client, "GET", MODELS_URL, headers=headers
                    )
        except AIProviderError as exc:
            return ValidateResult(
                ok=False, error=capped_http.validation_error(exc)
            )
        except (httpx.HTTPError, httpx.TimeoutException, TimeoutError) as exc:
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
        try:
            models = model_ids(payload)
        except TypeError:
            return ValidateResult(ok=False, error=UNEXPECTED_MODELS_RESPONSE)
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
                async with asyncio.timeout(CHAT_TIMEOUT_S):
                    resp = await capped_http.request(
                        client, "POST", CHAT_URL, headers=headers, json=body
                    )
        except (httpx.HTTPError, httpx.TimeoutException, TimeoutError) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise AIProviderError(
                code=f"provider_status_{resp.status_code}",
                status_code=resp.status_code,
                provider_reason=provider_reason(resp),
            )
        try:
            payload = resp.json()
        except ValueError:
            raise AIProviderError(code="provider_invalid_json") from None
        try:
            content = openai_message_content(payload)
            usage = usage_of(payload)
            prompt_tokens = token_count(usage.get("prompt_tokens"))
            completion_tokens = token_count(usage.get("completion_tokens"))
        except UNEXPECTED_SHAPE_ERRORS:
            raise AIProviderError(code="provider_unexpected_shape") from None
        return LLMResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=response_model(payload, model),
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
                async with asyncio.timeout(EMBED_TIMEOUT_S):
                    resp = await capped_http.request(
                        client, "POST", EMBEDDINGS_URL, headers=headers, json=body
                    )
        except (httpx.HTTPError, httpx.TimeoutException, TimeoutError) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise AIProviderError(
                code=f"provider_status_{resp.status_code}",
                status_code=resp.status_code,
                provider_reason=provider_reason(resp),
            )
        try:
            payload = resp.json()
        except ValueError:
            raise AIProviderError(code="provider_invalid_json") from None
        try:
            vectors = embedding_vectors(payload)
            usage = usage_of(payload)
            prompt_tokens = token_count(usage.get("prompt_tokens"))
        except UNEXPECTED_SHAPE_ERRORS:
            raise AIProviderError(code="provider_unexpected_shape") from None
        return EmbedResponse(
            vectors=vectors,
            model=response_model(payload, actual_model),
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
                async with asyncio.timeout(CHAT_TIMEOUT_S):
                    resp = await capped_http.request(
                        client, "POST", CHAT_URL, headers=headers, json=body
                    )
        except (httpx.HTTPError, httpx.TimeoutException, TimeoutError) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise AIProviderError(
                code=f"provider_status_{resp.status_code}",
                status_code=resp.status_code,
                provider_reason=provider_reason(resp),
            )
        try:
            payload = resp.json()
        except ValueError:
            raise AIProviderError(code="provider_invalid_json") from None
        try:
            content = openai_message_content(payload)
            usage = usage_of(payload)
            prompt_tokens = token_count(usage.get("prompt_tokens"))
            completion_tokens = token_count(usage.get("completion_tokens"))
        except UNEXPECTED_SHAPE_ERRORS:
            raise AIProviderError(code="provider_unexpected_shape") from None
        return LLMResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=response_model(payload, model),
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
        body: dict = {
            "model": model,
            "messages": to_openai_messages(messages),
            "tools": tools,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as client:
                async with asyncio.timeout(CHAT_TIMEOUT_S):
                    resp = await capped_http.request(
                        client, "POST", CHAT_URL, headers=headers, json=body
                    )
        except (httpx.HTTPError, httpx.TimeoutException, TimeoutError) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise AIProviderError(
                code=f"provider_status_{resp.status_code}",
                status_code=resp.status_code,
                provider_reason=provider_reason(resp),
            )
        try:
            payload = resp.json()
        except ValueError:
            raise AIProviderError(code="provider_invalid_json") from None
        try:
            message = payload["choices"][0]["message"]
            tool_calls = parse_openai_tool_calls(message)
            content = message.get("content")
            content = content if isinstance(content, str) else ""
            usage = usage_of(payload)
            prompt_tokens = token_count(usage.get("prompt_tokens"))
            completion_tokens = token_count(usage.get("completion_tokens"))
        except UNEXPECTED_SHAPE_ERRORS:
            raise AIProviderError(code="provider_unexpected_shape") from None
        return FunctionCallResponse(
            tool_calls=tool_calls,
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=response_model(payload, model),
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
        saw_done = False
        try:
            async with httpx.AsyncClient(timeout=STREAM_TIMEOUT_S) as client:
                async with client.stream(
                    "POST", CHAT_URL,
                    headers=capped_http.identity_headers(headers),
                    json=body,
                ) as resp:
                    if resp.status_code != 200:
                        raise AIProviderError(
                            code=f"provider_status_{resp.status_code}",
                            status_code=resp.status_code,
                        )
                    async for line in capped_http.aiter_lines(resp):
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            saw_done = True
                            break
                        try:
                            event = json.loads(data)
                        except (ValueError, RecursionError):
                            # malformed line (deep nesting included) — skip;
                            # per-line resilience
                            continue
                        try:
                            delta_text, usage = parse_openai_stream_event(event)
                            if usage is not None:
                                final_usage = TokenUsage(
                                    prompt_tokens=token_count(
                                        usage.get("prompt_tokens")
                                    ),
                                    completion_tokens=token_count(
                                        usage.get("completion_tokens")
                                    ),
                                )
                        except UNEXPECTED_SHAPE_ERRORS:
                            raise AIProviderError(
                                code="provider_unexpected_shape"
                            ) from None
                        if delta_text:
                            yield StreamChunk(delta_text=delta_text, done=False)
        except AIProviderError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        yield StreamChunk(
            delta_text="",
            done=True,
            final_usage=final_usage,
            usage_final=saw_done and final_usage is not None,
        )
