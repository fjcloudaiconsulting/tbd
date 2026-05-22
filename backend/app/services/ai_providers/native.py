"""TBD-native adapter shell (PR1 stub).

The spec ships the full scaffolding here in PR1 because the substrate
work for routing/caps/consent has to know "native is a thing that
exists in the registry" — but **no real native backend exists yet**.

Behavior contract (spec §5):

- ``AI_NATIVE_ENABLED=false`` (default): all methods raise
  ``NativeNotAvailable("not_yet_available")``. Selection endpoints map
  this to a typed refusal so a hand-rolled API client gets a
  machine-readable answer instead of a 500.
- ``AI_NATIVE_ENABLED=true``: PR1 STILL raises ``NativeNotAvailable``
  because no native backend exists. A structlog warning fires so
  operators see "gate is on but backend isn't ready" in their logs.
  PR4 ships the actual consent-gated dispatch; the real backend is a
  separate work item past PR4.

Credential creation for ``provider=native`` is independently rejected
at the service layer with a 400 / ``native_not_available`` code,
regardless of the gate, because there's nothing to store a credential
for yet.
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
