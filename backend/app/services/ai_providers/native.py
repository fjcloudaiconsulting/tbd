"""TBD-native adapter shell (PR1 stub).

Always raises ``NativeNotAvailable("not_yet_available")`` in PR1-PR3.
A future PR (PR4 or later) will gate this on ``AI_NATIVE_ENABLED`` once
a real native backend exists. The substrate work for routing/caps/
consent has to know "native is a thing that exists in the registry" —
hence this stub — but no real native backend ships in PR3.

Behavior contract (spec §5):

- Regardless of ``AI_NATIVE_ENABLED``, every capability call raises
  ``NativeNotAvailable("not_yet_available")``. The env flag is read
  only to emit an operator warning when it's been flipped on without a
  backend behind it.
- The ``/options`` endpoint mirrors this contract: native is reported
  with ``availability="not_yet_available"`` for PR1 regardless of the
  flag, so the UI never advertises an option the create path will
  refuse.
- Credential creation for ``provider=native`` is independently
  rejected at the service layer with a 400 / ``native_not_available``
  code, regardless of the gate, because there's nothing to store a
  credential for yet.
"""
from __future__ import annotations

import structlog

from app.config import settings
from app.services.ai_providers.base import NativeNotAvailable, ValidateResult


logger = structlog.stdlib.get_logger()


class NativeAdapter:
    """Stub adapter — raises until a native backend exists."""

    def __init__(self) -> None:
        if settings.ai_native_enabled:
            logger.warning(
                "ai.native.gate_on_but_backend_missing",
                ai_native_enabled=True,
                note="AI_NATIVE_ENABLED=true but no native backend ships in PR3",
            )

    async def validate(self) -> ValidateResult:
        # Not actually called in PR1 because credential creation refuses
        # native earlier, but implementing the contract keeps the
        # registry symmetric and lets PR4 plug in real validation
        # without changing the call sites.
        raise NativeNotAvailable("not_yet_available")

    async def chat(self, *, model, messages, max_tokens=None):
        # PR4 will gate this on ``AI_NATIVE_ENABLED`` once a real native
        # backend exists. For PR2/PR3 the adapter is wired into the
        # registry so the dispatch layer is symmetric, but the chat
        # path still refuses immediately.
        raise NativeNotAvailable("not_yet_available")

    async def embed(self, *, texts, model=None):
        raise NativeNotAvailable("not_yet_available")

    async def chat_structured(self, *, model, messages, schema, max_tokens=None):
        raise NativeNotAvailable("not_yet_available")

    async def function_call(self, *, model, messages, tools, max_tokens=None):
        raise NativeNotAvailable("not_yet_available")

    async def stream(self, *, model, messages, max_tokens=None):
        # Note: this is a coroutine that, when called, should return an
        # async iterator. To preserve symmetry with the protocol, we
        # raise here — the call_llm_stream wrapper handles the "raise
        # before iteration" shape.
        raise NativeNotAvailable("not_yet_available")
        # Make pyright happy:
        yield  # pragma: no cover  # noqa: B901
