"""Capability protocols + adapter factory for BYO AI providers.

PR1 shipped ``ValidateCapable`` only. PR2 added ``ChatCapable.chat`` +
``LLMResponse`` + ``AIProviderError``. PR3 (this layer) implements the
remaining capability protocols:

- ``EmbedCapable.embed`` — vector embedding for OpenAI, Ollama,
  OpenAI-compatible. Anthropic does not expose embeddings; the adapter
  raises ``NotImplementedError`` so callers can fall back. A future PR
  may add Voyage AI as a sibling provider.
- ``StructuredOutputCapable.chat_structured`` — JSON-mode + schema
  validation, with the architect-locked retry cap of 2 (3 attempts
  total). Adapters return the raw provider response; the service layer
  validates against the schema and applies the retry budget. On
  exhaustion the service raises ``StructuredOutputError`` with code
  ``STATUS_ERROR_STRUCTURED_OUTPUT``.
- ``FunctionCallCapable.function_call`` — provider-side tool use
  (OpenAI function-calling shape, Anthropic tool_use). Ollama is
  model-dependent; the adapter raises ``CapabilityNotSupported`` for
  models we know don't support it.
- ``StreamCapable.stream`` — async-iterator streaming of chat
  responses. The ledger is written once at end-of-stream with the
  final token usage (estimated if the provider doesn't report tokens
  for the streamed response).

The Native adapter still raises ``NativeNotAvailable`` for every
capability, including the PR3 ones, until PR4 wires a real backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

from app.models.org_ai_credential import AiProvider


@dataclass
class ValidateResult:
    ok: bool
    error: Optional[str] = None
    discovered_models: list[str] = field(default_factory=list)
    discovered_capabilities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LLMResponse:
    """Provider-neutral chat response.

    ``content`` is the assistant message text. ``prompt_tokens`` /
    ``completion_tokens`` feed the cap accounting (``est_cost_cents``
    is computed downstream by ``ai_pricing.estimate_cost_cents``).
    ``model`` echoes the model the adapter actually called — useful
    in the ledger for ops debugging when an adapter falls back to a
    different model than the routing requested (e.g. Ollama with a
    missing pinned model).
    """

    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str


@dataclass(frozen=True)
class EmbedResponse:
    """Provider-neutral embedding response.

    ``vectors`` is one float vector per input text (same order as the
    request). ``prompt_tokens`` feeds the ledger / cap accounting;
    embedding APIs charge per-input-token only (no completion side).
    """

    vectors: list[list[float]]
    model: str
    prompt_tokens: int


@dataclass(frozen=True)
class StructuredResponse:
    """Provider-neutral structured-output response.

    ``parsed`` is the JSON object after schema validation; ``raw_text``
    is the raw assistant text (kept for forensic logging). ``retries_used``
    is 0, 1, or 2 — the count of retries the SERVICE layer needed before
    the parse succeeded. Architect lock #13: max 2 retries (3 total
    attempts) before ``STATUS_ERROR_STRUCTURED_OUTPUT``.
    """

    parsed: dict
    raw_text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    retries_used: int


@dataclass(frozen=True)
class FunctionCallResponse:
    """Provider-neutral function-call response.

    ``tool_calls`` is the structured list of tool invocations the model
    requested. Each entry has ``name`` (the tool name) and
    ``arguments`` (a dict — already JSON-parsed). ``content`` is any
    free-text the model emitted alongside the tool call (typically
    empty when a tool was invoked).
    """

    tool_calls: list[dict]
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str


@dataclass(frozen=True)
class TokenUsage:
    """Token usage summary attached to the final stream chunk."""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class StreamChunk:
    """One chunk of a streamed chat response.

    ``delta_text`` is the incremental text since the previous chunk.
    ``done`` is True for the final synthetic chunk (which carries
    ``final_usage``); all other chunks have ``done=False`` and
    ``final_usage=None``.
    """

    delta_text: str
    done: bool
    final_usage: Optional[TokenUsage] = None


class NativeNotAvailable(Exception):
    """Raised by the native adapter while AI_NATIVE_ENABLED is False.

    PR4 keeps raising this even when the gate is true until a real
    native backend exists (spec §5: "the toggle is a one-way decision
    per environment"). Selection endpoints map this to the
    ``not_yet_available`` typed code; ``call_llm`` maps it to
    ``ai_native_not_available``.
    """

    def __init__(self, code: str = "not_yet_available") -> None:
        super().__init__(code)
        self.code = code


class AIProviderError(Exception):
    """Adapter-side failure surface.

    Wraps any network/4xx/5xx/timeout exception from a provider into
    a typed error with a **sanitized** message. The raw provider
    response body and the exception's own repr are NEVER carried
    through — a hostile OAI-compatible endpoint can mirror request
    headers back, leaking the plaintext API key that just left this
    process. Same posture as PR1's ``validate`` paths.
    """

    def __init__(self, *, code: str, status_code: Optional[int] = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class StructuredOutputError(Exception):
    """Service-layer failure after the architect-locked retry budget.

    Architect lock #13: max 2 retries (3 total attempts) on JSON
    parse / schema-validation failure before this fires. The ledger
    row still gets written (with ``retries_used=2``, success=False,
    error_class=``STATUS_ERROR_STRUCTURED_OUTPUT``) so the failed
    attempts count against the cap.
    """

    def __init__(self, code: str = "STATUS_ERROR_STRUCTURED_OUTPUT") -> None:
        super().__init__(code)
        self.code = code


class CapabilityNotSupported(Exception):
    """Adapter refuses a capability the static class flag advertises.

    Used by Ollama's ``function_call`` when the requested model isn't
    known to support tool use — Ollama exposes function calling per
    model, not per server. Callers (the service layer) decide whether
    to fall back to free-text JSON, a smaller model, or surface a 412
    to the user.
    """

    def __init__(self, *, model: str, capability: str = "function_call") -> None:
        super().__init__(f"{capability} not supported by model {model!r}")
        self.model = model
        self.capability = capability


@runtime_checkable
class ValidateCapable(Protocol):
    async def validate(self) -> ValidateResult:
        ...


@runtime_checkable
class ChatCapable(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        ...


@runtime_checkable
class EmbedCapable(Protocol):
    async def embed(
        self,
        *,
        texts: list[str],
        model: Optional[str] = None,
    ) -> EmbedResponse:
        ...


@runtime_checkable
class StructuredOutputCapable(Protocol):
    """Adapter side of structured output.

    Returns the raw provider response (text). The SERVICE layer
    validates against the schema and applies the architect-locked
    retry cap. Keeping the schema enforcement out of the adapter lets
    the retry counter live in one place (``call_llm_structured``) and
    keeps adapters thin.
    """

    async def chat_structured(
        self,
        *,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        ...


@runtime_checkable
class StreamCapable(Protocol):
    async def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamChunk]:
        ...


@runtime_checkable
class FunctionCallCapable(Protocol):
    async def function_call(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: Optional[int] = None,
    ) -> FunctionCallResponse:
        ...


def get_adapter(
    provider: AiProvider,
    *,
    api_key: str,
    bearer_token: Optional[str] = None,
    base_url: Optional[str] = None,
) -> ValidateCapable:
    """Return a ValidateCapable adapter for ``provider``.

    Imports are local so the package init stays light and so the test
    suite can monkeypatch individual adapter modules without dragging
    them all in.
    """
    if provider == AiProvider.OPENAI:
        from app.services.ai_providers.openai import OpenAIAdapter
        return OpenAIAdapter(api_key=api_key)
    if provider == AiProvider.ANTHROPIC:
        from app.services.ai_providers.anthropic import AnthropicAdapter
        return AnthropicAdapter(api_key=api_key)
    if provider == AiProvider.OLLAMA:
        from app.services.ai_providers.ollama import OllamaAdapter
        if not base_url:
            raise ValueError("base_url required for ollama provider")
        return OllamaAdapter(
            base_url=base_url,
            api_key=api_key,
            bearer_token=bearer_token,
        )
    if provider == AiProvider.OPENAI_COMPATIBLE:
        from app.services.ai_providers.openai_compatible import (
            OpenAICompatibleAdapter,
        )
        if not base_url:
            raise ValueError("base_url required for openai_compatible provider")
        return OpenAICompatibleAdapter(api_key=api_key, base_url=base_url)
    if provider == AiProvider.NATIVE:
        # The credential service refuses creation for native earlier
        # (PR1 has no native backend), so this branch is dead code for
        # PR1. Keeping it wired in keeps the registry symmetric — PR4's
        # gate flip uses the same factory.
        from app.services.ai_providers.native import NativeAdapter
        return NativeAdapter()
    raise ValueError(f"Unknown AI provider: {provider!r}")
