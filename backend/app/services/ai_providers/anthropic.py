"""Anthropic adapter — validate via GET /v1/models, chat via /v1/messages.

PR3:
- ``embed`` raises ``NotImplementedError`` — Anthropic does NOT expose
  a public embeddings API. A future PR may add Voyage AI as a sibling
  provider (Voyage was an Anthropic-recommended embedding partner
  before Anthropic acquired it; v1 ships without it).
- ``chat_structured`` uses the tool-use-as-JSON pattern: a single tool
  with ``input_schema`` set to the response schema. The model returns
  a ``tool_use`` block whose ``input`` is the parsed JSON.
- ``function_call`` uses Anthropic's tool-use API natively. Tools are
  normalized from the OpenAI shape (``{type:"function", function:{...}}``)
  into Anthropic's flat shape (``{name, description, input_schema}``).
- ``stream`` uses the Anthropic SSE messages stream API.
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
STREAM_TIMEOUT_S = 60.0
# Anthropic advertises chat + tool use + structured output + streaming.
# ``embed`` is intentionally absent — Anthropic has no embeddings API.
# Orgs wanting embeddings must add a sibling Voyage AI credential
# (the embed() method on this adapter raises NotImplementedError).
DEFAULT_CAPABILITIES = ["chat", "structured_output", "function_call", "stream"]
MODELS_URL = "https://api.anthropic.com/v1/models"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Anthropic requires max_tokens on every messages call.
DEFAULT_CHAT_MAX_TOKENS = 1024


def _split_system(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """Split a chat history into (system_parts, non_system_turns).

    Anthropic separates ``system`` from the user/assistant turns —
    keeping the split logic centralized so chat/structured/stream
    share the same shape transformation.
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
    return system_parts, chat_messages


def _normalize_tool_for_anthropic(tool: dict) -> dict:
    """Accept OpenAI-shape tools and emit Anthropic-shape tools.

    Caller is expected to pass the canonical OpenAI shape
    ``{"type": "function", "function": {"name": ..., "description": ...,
    "parameters": {...}}}``. We map it to Anthropic's
    ``{"name": ..., "description": ..., "input_schema": {...}}``.

    Tools that are already in Anthropic shape (have ``input_schema``)
    are passed through unchanged so callers can mix shapes.
    """
    if "input_schema" in tool:
        return tool
    inner = tool.get("function") or {}
    return {
        "name": inner.get("name", ""),
        "description": inner.get("description", ""),
        "input_schema": inner.get("parameters") or {"type": "object"},
    }


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
        """POST /v1/messages. Maps response.content[*].text -> content."""
        system_parts, chat_messages = _split_system(messages)
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

    async def embed(
        self,
        *,
        texts: list[str],
        model: Optional[str] = None,
    ) -> EmbedResponse:
        """Anthropic does NOT expose a public embeddings API.

        Documented refusal at the protocol layer so callers can detect
        and fall back. A future PR may add Voyage AI as a sibling
        provider entry.
        """
        raise NotImplementedError(
            "Anthropic does not expose embeddings API"
        )

    async def chat_structured(
        self,
        *,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Anthropic structured output via tool-use-as-JSON.

        We declare a single tool whose ``input_schema`` is the response
        schema and force the model to call it. The tool_use block's
        ``input`` is the parsed JSON; we serialize it back to text in
        ``content`` so the service layer's schema validator sees the
        same string shape across all adapters.
        """
        system_parts, chat_messages = _split_system(messages)
        tool = {
            "name": "respond_structured",
            "description": "Return the response as a JSON object matching the schema.",
            "input_schema": schema,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens or DEFAULT_CHAT_MAX_TOKENS,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": "respond_structured"},
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
            tool_input: Optional[dict] = None
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = b.get("input")
                    if isinstance(inp, dict):
                        tool_input = inp
                        break
            # No tool_use block -> the service-layer retry budget will
            # catch the empty-content case as a JSON parse failure.
            content_text = json.dumps(tool_input) if tool_input is not None else ""
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

    async def function_call(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: Optional[int] = None,
    ) -> FunctionCallResponse:
        """POST /v1/messages with tool_use enabled.

        Caller can pass either OpenAI-shape tools or native Anthropic
        tools; both are normalized to Anthropic's flat shape.
        """
        system_parts, chat_messages = _split_system(messages)
        normalized_tools = [_normalize_tool_for_anthropic(t) for t in tools]
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens or DEFAULT_CHAT_MAX_TOKENS,
            "tools": normalized_tools,
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
            tool_calls: list[dict] = []
            text_parts: list[str] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input")
                    tool_calls.append(
                        {
                            "name": str(b.get("name") or ""),
                            "arguments": inp if isinstance(inp, dict) else {},
                        }
                    )
                elif b.get("type") == "text":
                    text_parts.append(str(b.get("text") or ""))
            usage = payload.get("usage", {}) or {}
            prompt_tokens = int(usage.get("input_tokens", 0) or 0)
            completion_tokens = int(usage.get("output_tokens", 0) or 0)
        except (KeyError, TypeError):
            raise AIProviderError(code="provider_unexpected_shape") from None
        return FunctionCallResponse(
            tool_calls=tool_calls,
            content="".join(text_parts),
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
        """POST /v1/messages with ``stream: true`` (SSE).

        Anthropic SSE uses ``event:`` framing. We care about:
        - ``content_block_delta`` (delta.text)        → delta_text chunk
        - ``message_start`` (usage.input_tokens)      → prompt tokens
        - ``message_delta`` (usage.output_tokens)     → completion tokens

        Final ``StreamChunk(done=True, final_usage=...)`` is emitted
        after the stream closes.
        """
        system_parts, chat_messages = _split_system(messages)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body: dict = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens or DEFAULT_CHAT_MAX_TOKENS,
            "stream": True,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)

        prompt_tokens = 0
        completion_tokens = 0
        try:
            async with httpx.AsyncClient(timeout=STREAM_TIMEOUT_S) as client:
                async with client.stream(
                    "POST", MESSAGES_URL, headers=headers, json=body
                ) as resp:
                    if resp.status_code != 200:
                        raise AIProviderError(
                            code=f"provider_status_{resp.status_code}",
                            status_code=resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data:
                            continue
                        try:
                            event = json.loads(data)
                        except ValueError:
                            continue
                        etype = event.get("type")
                        if etype == "content_block_delta":
                            delta = event.get("delta") or {}
                            text = str(delta.get("text") or "")
                            if text:
                                yield StreamChunk(
                                    delta_text=text, done=False
                                )
                        elif etype == "message_start":
                            msg = event.get("message") or {}
                            usage = msg.get("usage") or {}
                            prompt_tokens = int(
                                usage.get("input_tokens", 0) or 0
                            )
                            completion_tokens = int(
                                usage.get("output_tokens", 0) or 0
                            )
                        elif etype == "message_delta":
                            usage = event.get("usage") or {}
                            completion_tokens = int(
                                usage.get("output_tokens", completion_tokens)
                                or completion_tokens
                            )
        except AIProviderError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        yield StreamChunk(
            delta_text="",
            done=True,
            final_usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )
