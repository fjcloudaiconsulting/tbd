"""Capability protocols + adapter factory for BYO AI providers.

PR1 shipped ``ValidateCapable`` only. PR2 adds:

- ``LLMResponse`` — provider-neutral chat response shape consumed by
  the dispatch chokepoint (``call_llm`` in ``ai_dispatch``).
- ``AIProviderError`` — typed wrapper raised by every adapter when
  the provider call fails. The wrapped message is **sanitized** (no
  provider response body), per the PR1 SSRF / sanitization lock.
- ``ChatCapable.chat`` — protocol signature for the chat dispatch.
  OpenAI, Anthropic, Ollama, and OpenAI-compatible adapters implement
  it as a thin pass-through; Native still raises ``NativeNotAvailable``.
- ``StructuredOutputCapable.chat_structured`` — protocol signature
  only. The implementation is deferred to PR3 (architect lock: retry
  cap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

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
    ...  # implemented in PR3+


@runtime_checkable
class StructuredOutputCapable(Protocol):
    """Protocol signature only — implementation deferred to PR3.

    Architect lock: ``chat_structured`` ships in PR3 with the
    documented retry cap of 2 on JSON parse / schema-validation
    failure. Defining the signature here lets PR3 add adapter
    implementations without re-shaping the protocol layer.
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
    ...  # implemented in PR3+


@runtime_checkable
class FunctionCallCapable(Protocol):
    ...  # implemented in PR3+


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
