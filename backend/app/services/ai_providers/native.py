"""TBD-native adapter shell (PR1 stub).

Always returns ``not_yet_available`` in PR1. A future PR will gate this
on ``AI_NATIVE_ENABLED`` once a real native backend exists. The
substrate work for routing/caps/consent has to know "native is a thing
that exists in the registry" — hence this stub — but no real native
backend ships in PR1.

Behavior contract (spec §5):

- Regardless of ``AI_NATIVE_ENABLED``, ``validate()`` raises
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
                note="AI_NATIVE_ENABLED=true but no native backend ships in PR1",
            )

    async def validate(self) -> ValidateResult:
        # Not actually called in PR1 because credential creation refuses
        # native earlier, but implementing the contract keeps the
        # registry symmetric and lets PR4 plug in real validation
        # without changing the call sites.
        raise NativeNotAvailable("not_yet_available")
