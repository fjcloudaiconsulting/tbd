"""Ollama adapter — validates by GET {base_url}/api/tags.

PR3:
- ``embed`` POSTs to ``/api/embeddings``.
- ``chat_structured`` uses Ollama's ``format: "json"`` option plus
  schema-in-system-message. The service layer enforces the retry cap.
- ``function_call`` raises ``CapabilityNotSupported`` for models that
  don't advertise tool use. Newer Ollama builds support OpenAI-shape
  tools on a curated allowlist (llama3.1/3.2, mistral-nemo, etc.).
  Rather than maintain a copy of that allowlist, we refuse by default
  and let the routing capability check direct callers to a provider
  that does support it. Models that DO support it can be allowlisted
  via the ``KNOWN_FUNCTION_CALL_MODELS`` prefix list.
- ``stream`` POSTs to ``/api/chat`` with ``stream: true``; Ollama
  emits NDJSON (one JSON object per line) rather than SSE.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from app.services.ai_providers.base import (
    AIProviderError,
    CapabilityNotSupported,
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
# Baseline Ollama capabilities. ``function_call`` and
# ``structured_output`` are conditional on whether any discovered model
# matches the ``KNOWN_FUNCTION_CALL_MODELS`` prefix list — see
# ``validate()`` below.
BASELINE_CAPABILITIES = ["chat", "embed", "stream"]

# Models Ollama is known to expose tool-calling on (best-effort —
# refresh during the same quarterly window that touches the pricing
# table). Caller can extend by passing through a provider that
# reports its own capability via discovered_capabilities.
KNOWN_FUNCTION_CALL_MODELS = (
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "mistral-nemo",
    "mistral-large",
    "command-r",
    "firefunction",
    "qwen2.5",
)


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
        capabilities = list(BASELINE_CAPABILITIES)
        # Ollama exposes function-calling + structured output only on a
        # curated allowlist of models (llama3.1+, mistral-nemo, etc.).
        # Advertise the capabilities only when at least one discovered
        # model matches an allowlisted prefix.
        if any(
            m.startswith(prefix)
            for m in models
            for prefix in KNOWN_FUNCTION_CALL_MODELS
        ):
            capabilities.append("function_call")
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
        """POST {base_url}/api/chat with stream=False."""
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

    async def embed(
        self,
        *,
        texts: list[str],
        model: Optional[str] = None,
    ) -> EmbedResponse:
        """POST {base_url}/api/embeddings — one POST per input text.

        Ollama's ``/api/embeddings`` accepts a single prompt; we
        sequentially fire one request per text to keep the contract
        symmetric with the OpenAI batch shape. The vector order
        matches the input order.
        """
        if not model:
            raise AIProviderError(code="ollama_embed_model_required")
        headers = {**self._headers(), "Content-Type": "application/json"}
        url = f"{self.base_url}/api/embeddings"
        vectors: list[list[float]] = []
        actual_model = model
        try:
            async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as client:
                for text in texts:
                    resp = await client.post(
                        url,
                        headers=headers,
                        json={"model": model, "prompt": text},
                    )
                    if resp.status_code != 200:
                        raise AIProviderError(
                            code=f"provider_status_{resp.status_code}",
                            status_code=resp.status_code,
                        )
                    try:
                        payload = resp.json()
                    except ValueError:
                        raise AIProviderError(
                            code="provider_invalid_json"
                        ) from None
                    embedding = payload.get("embedding")
                    if not isinstance(embedding, list):
                        raise AIProviderError(
                            code="provider_unexpected_shape"
                        ) from None
                    vectors.append([float(x) for x in embedding])
                    actual_model = payload.get("model", model) or model
        except AIProviderError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise AIProviderError(
                code=f"network_{type(exc).__name__}"
            ) from None
        return EmbedResponse(
            vectors=vectors,
            model=actual_model,
            # Ollama's embeddings endpoint does NOT report a token
            # count; estimate via 4 chars / token to keep the cap
            # accounting non-zero.
            prompt_tokens=max(1, sum(len(t) for t in texts) // 4),
        )

    async def chat_structured(
        self,
        *,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Ollama structured output via ``format: "json"`` + system
        message describing the schema. The service layer's retry budget
        catches the cases where the model emits malformed JSON or a
        JSON that fails schema validation.
        """
        headers = {**self._headers(), "Content-Type": "application/json"}
        schema_hint = (
            "Output ONLY a JSON object matching this schema: "
            + json.dumps(schema, sort_keys=True)
        )
        # Prepend the schema hint as a system message so it sits ahead
        # of any caller-supplied system messages without clobbering
        # them.
        prepended = [{"role": "system", "content": schema_hint}] + list(messages)
        body: dict = {
            "model": model,
            "messages": prepended,
            "stream": False,
            "format": "json",
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

    async def function_call(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: Optional[int] = None,
    ) -> FunctionCallResponse:
        """Ollama function-calling is per-model.

        We refuse with ``CapabilityNotSupported`` for any model whose
        name doesn't start with one of the known function-calling
        prefixes. The caller (service layer) is expected to surface a
        412 ``ai_capability_not_supported`` so the user reconfigures
        routing.

        For supported models we POST to ``/api/chat`` with the tools
        array passed through (Ollama accepts OpenAI-shape tools on
        function-calling-capable models since v0.3+).
        """
        if not any(
            model.startswith(prefix) for prefix in KNOWN_FUNCTION_CALL_MODELS
        ):
            raise CapabilityNotSupported(
                model=model, capability="function_call"
            )
        headers = {**self._headers(), "Content-Type": "application/json"}
        body: dict = {
            "model": model,
            "messages": messages,
            "tools": tools,
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
            raw_tool_calls = message.get("tool_calls") or []
            tool_calls: list[dict] = []
            for call in raw_tool_calls:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (TypeError, ValueError):
                        args = {}
                tool_calls.append(
                    {
                        "name": fn.get("name") or "",
                        "arguments": args if isinstance(args, dict) else {},
                    }
                )
            content = str(message.get("content") or "")
            prompt_tokens = int(payload.get("prompt_eval_count", 0) or 0)
            completion_tokens = int(payload.get("eval_count", 0) or 0)
        except (KeyError, TypeError):
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
        """POST {base_url}/api/chat with ``stream: true``.

        Ollama streams NDJSON (one JSON object per line, not SSE).
        Each line carries an incremental ``message.content`` delta plus
        a ``done`` flag; the final line has ``done: true`` and the
        token counts.
        """
        headers = {**self._headers(), "Content-Type": "application/json"}
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if max_tokens is not None:
            body["options"] = {"num_predict": max_tokens}
        url = f"{self.base_url}/api/chat"

        prompt_tokens = 0
        completion_tokens = 0
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
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        msg = event.get("message") or {}
                        delta = str(msg.get("content") or "")
                        if delta:
                            yield StreamChunk(delta_text=delta, done=False)
                        if event.get("done"):
                            prompt_tokens = int(
                                event.get("prompt_eval_count", 0) or 0
                            )
                            completion_tokens = int(
                                event.get("eval_count", 0) or 0
                            )
                            break
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
