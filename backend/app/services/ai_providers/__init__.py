"""Provider adapter package for BYO AI credentials (PR1).

PR1 ships only the ``ValidateCapable`` protocol — adapters reach out to
each provider's ``/models`` endpoint to confirm the key works and
discover available models. The other Capable protocols
(``ChatCapable``, ``EmbedCapable``, ``StructuredOutputCapable``,
``StreamCapable``, ``FunctionCallCapable``) are declared as empty
``Protocol`` stubs so type-checking lights up in later PRs without a
follow-up refactor of this module.

Distinct from the older ``ai_adapters`` package (LAI foundation mock
adapter) — that one is the call_llm() chokepoint; this one is the
provider abstraction for BYO credentials.
"""
from app.services.ai_providers.base import (
    ChatCapable,
    EmbedCapable,
    FunctionCallCapable,
    StreamCapable,
    StructuredOutputCapable,
    ValidateCapable,
    ValidateResult,
    get_adapter,
)

__all__ = [
    "ChatCapable",
    "EmbedCapable",
    "FunctionCallCapable",
    "StreamCapable",
    "StructuredOutputCapable",
    "ValidateCapable",
    "ValidateResult",
    "get_adapter",
]
