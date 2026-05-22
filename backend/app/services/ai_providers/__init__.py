"""Provider adapter package for BYO AI credentials.

PR1 shipped the ``ValidateCapable`` protocol. PR2 adds:

- ``ChatCapable.chat`` — implementations on OpenAI, Anthropic,
  Ollama, and OpenAI-compatible adapters.
- ``LLMResponse`` — provider-neutral response shape.
- ``AIProviderError`` — typed wrapper with a sanitized message.
- ``StructuredOutputCapable.chat_structured`` — protocol signature
  only (implementations deferred to PR3 per architect lock).

Distinct from the older ``ai_adapters`` package (LAI foundation mock
adapter) — that one is the call_llm() chokepoint; this one is the
provider abstraction for BYO credentials.
"""
from app.services.ai_providers.base import (
    AIProviderError,
    ChatCapable,
    EmbedCapable,
    FunctionCallCapable,
    LLMResponse,
    NativeNotAvailable,
    StreamCapable,
    StructuredOutputCapable,
    ValidateCapable,
    ValidateResult,
    get_adapter,
)

__all__ = [
    "AIProviderError",
    "ChatCapable",
    "EmbedCapable",
    "FunctionCallCapable",
    "LLMResponse",
    "NativeNotAvailable",
    "StreamCapable",
    "StructuredOutputCapable",
    "ValidateCapable",
    "ValidateResult",
    "get_adapter",
]
