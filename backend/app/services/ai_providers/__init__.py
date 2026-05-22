"""Provider adapter package for BYO AI credentials.

PR1 shipped the ``ValidateCapable`` protocol. PR2 added ``ChatCapable``,
``LLMResponse``, ``AIProviderError``. PR3 adds the remaining
capabilities:

- ``EmbedCapable.embed`` — OpenAI, Ollama, OpenAI-compatible (Anthropic
  raises NotImplementedError).
- ``StructuredOutputCapable.chat_structured`` — adapter-side
  implementation; service layer applies the architect-locked retry cap
  via ``ai_dispatch.call_llm_structured``.
- ``FunctionCallCapable.function_call`` — OpenAI/Anthropic adapters;
  Ollama raises ``CapabilityNotSupported`` for non-tool-using models.
- ``StreamCapable.stream`` — async iterator over ``StreamChunk``s.

Native still raises ``NativeNotAvailable`` for every capability until
PR4 wires a real backend.

Distinct from the older ``ai_adapters`` package (LAI foundation mock
adapter) — that one is the call_llm() chokepoint; this one is the
provider abstraction for BYO credentials.
"""
from app.services.ai_providers.base import (
    AIProviderError,
    CapabilityNotSupported,
    ChatCapable,
    EmbedCapable,
    EmbedResponse,
    FunctionCallCapable,
    FunctionCallResponse,
    LLMResponse,
    NativeNotAvailable,
    StreamCapable,
    StreamChunk,
    StructuredOutputCapable,
    StructuredOutputError,
    StructuredResponse,
    TokenUsage,
    ValidateCapable,
    ValidateResult,
    get_adapter,
)

__all__ = [
    "AIProviderError",
    "CapabilityNotSupported",
    "ChatCapable",
    "EmbedCapable",
    "EmbedResponse",
    "FunctionCallCapable",
    "FunctionCallResponse",
    "LLMResponse",
    "NativeNotAvailable",
    "StreamCapable",
    "StreamChunk",
    "StructuredOutputCapable",
    "StructuredOutputError",
    "StructuredResponse",
    "TokenUsage",
    "ValidateCapable",
    "ValidateResult",
    "get_adapter",
]
