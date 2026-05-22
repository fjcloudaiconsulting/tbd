"""Capability protocols + adapter factory for BYO AI providers (PR1).

Only ``ValidateCapable`` has a real implementation today; the other
protocols are empty stubs reserved for PR3+ (chat / embed / function
calling / streaming / structured output).
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


@runtime_checkable
class ValidateCapable(Protocol):
    async def validate(self) -> ValidateResult:
        ...


# ---------------------------------------------------------------------
# Stubs reserved for PR3+. Empty Protocol bodies — implementations land
# alongside the per-feature surfaces that need them.
# ---------------------------------------------------------------------

@runtime_checkable
class ChatCapable(Protocol):
    ...  # implemented in PR3+


@runtime_checkable
class EmbedCapable(Protocol):
    ...  # implemented in PR3+


@runtime_checkable
class StructuredOutputCapable(Protocol):
    ...  # implemented in PR3+


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
