"""LAI Foundation — provider-neutral AI service boundary (PR-B).

Spec: ``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-14-lai-foundation.md``

This module exposes the single chokepoint ``call_llm()`` that every LAI
feature surface must call. Three properties this gives us:

1. **Feature gate is defense-in-depth.** Routers should already gate on
   ``require_feature(key)``; ``call_llm`` re-checks via
   ``has_feature(db, org_id, feature_key)`` and refuses if the gate is
   closed. A future caller that forgets the dependency-level check still
   cannot make an LLM call without an explicit org grant.

2. **Provider is per-org and defaults to mock.** Adapter selection comes
   from ``org_settings.ai.provider`` (defaults ``"mock"``). PR-B ships
   only the mock adapter; Anthropic/OpenAI adapters land in a separate
   sign-off-gated PR. There is no way in this module to reach a real
   network call.

3. **Prompts are pre-redacted contracts.** ``Prompt`` requires
   ``redaction_certified=True`` and refuses ``user_context`` containing
   PII-shaped keys (IBAN, account_number, full_name, ssn, tax_id). The
   redaction itself is the caller's responsibility; this is the
   compile-time tripwire.

Cap enforcement and the ``ai_usage`` ledger ship in PR-C (migration
048). In PR-B the cap-check is a stub that always returns "within
budget", and the only persisted artifact is the ``ai.call`` structlog
event.

Privacy invariants (locked by spec §8):
- No prompt content, no completion content, no API-key fragments in any
  structlog event from this module.
- ``user_context`` PII-key rejection is defense-in-depth, not the
  primary control — callers are still responsible for redaction.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.feature_catalog import ALL_FEATURE_KEYS, FeatureKey
from app.services import feature_service
from app.services.ai_adapters import mock_adapter
from app.services.exceptions import ValidationError


logger = structlog.stdlib.get_logger()


# Status enumeration matches spec §3 (ai_usage.status). PR-B only emits
# ``success``, ``dry_run``, and ``rejected_gate_closed`` — the cap and
# provider-error statuses are reserved for PR-C / the real-adapter PR.
STATUS_SUCCESS = "success"
STATUS_DRY_RUN = "dry_run"
STATUS_REJECTED_GATE_CLOSED = "rejected_gate_closed"
STATUS_REJECTED_OVER_CAP = "rejected_over_cap"
STATUS_ERROR_UNCONFIGURED = "error_unconfigured"
STATUS_ERROR_PROVIDER = "error_provider"
STATUS_ERROR_TIMEOUT = "error_timeout"
STATUS_ERROR_CONTENT_POLICY = "error_content_policy"


# PII key sentinel — defense-in-depth filter on ``Prompt.user_context``.
# Matches keys whose name *looks like* it carries raw PII. Adversarial
# inputs that hide PII in differently-named keys are not caught here;
# redaction at the caller is the primary control.
#
# The pattern matches snake_case (``full_name``) and CamelCase
# (``FullName``) variants — the optional ``[_]?`` separator lets
# ``fullname``, ``full_name``, ``FullName`` all trip the filter.
_PII_KEY_REGEX = re.compile(
    r"(iban|account[_]?number|full[_]?name|ssn|tax[_]?id)",
    re.IGNORECASE,
)


class PromptNotRedacted(Exception):
    """Programmer error: caller forgot to set ``redaction_certified=True``.

    Surfaces as HTTP 500 — bad input from app code is an operational
    alert, not a user-facing validation error. Same pattern as
    ``UnknownFeatureKey``.
    """

    def __init__(self) -> None:
        super().__init__("Prompt must be redacted before reaching ai_service")


class PromptContainsPII(Exception):
    """Programmer error: ``Prompt.user_context`` contains a key whose name
    matches the PII sentinel regex. The caller must redact before the
    prompt reaches this layer.

    Surfaces as HTTP 500. Same rationale as ``PromptNotRedacted``.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Prompt.user_context contains PII-shaped key: {key!r}")


class FeatureCapped(Exception):
    """Org has exceeded its monthly AI usage cap. Reserved for PR-C;
    PR-B never raises this because the cap-check is a stub. Routers map
    this to HTTP 402 ("Payment Required") per spec D2.
    """

    def __init__(self, org_id: int, feature_key: str) -> None:
        self.org_id = org_id
        self.feature_key = feature_key
        super().__init__(
            f"Org {org_id} is over the monthly AI usage cap for {feature_key}"
        )


class FeatureNotEnabled(Exception):
    """The feature gate is closed for this org. Routers should already
    have returned 403 via ``require_feature``; this is the defense-in-
    depth raise from inside ``call_llm``. Maps to HTTP 403.
    """

    def __init__(self, org_id: int, feature_key: str) -> None:
        self.org_id = org_id
        self.feature_key = feature_key
        super().__init__(
            f"Feature {feature_key!r} is not enabled for org {org_id}"
        )


@dataclass(frozen=True)
class Prompt:
    """Pre-redacted, typed prompt object.

    ``redaction_certified`` is a contract: the caller asserts that
    ``system_instructions`` and every value in ``user_context`` has been
    redacted. The service refuses prompts where this flag is False.

    The contract is intentionally not a runtime audit — we cannot
    reliably scan adversarial text for IBANs / account numbers / full
    names. The ``user_context`` PII-key check is defense-in-depth on
    the *structure* of the dict, not its content.
    """

    system_instructions: str
    user_context: dict[str, Any] = field(default_factory=dict)
    redaction_certified: bool = False


@dataclass(frozen=True)
class LLMResult:
    """Provider-neutral call result.

    ``request_id`` is a service-generated UUID. It is stamped into the
    ``ai_usage`` ledger row (PR-C) AND returned to the caller so any
    downstream ``ai.feedback.*`` audit event (PR-D) can correlate back
    to the original call.
    """

    content: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_cents: int
    dry_run: bool
    provider: str
    model: str
    status: str
    request_id: str


def _validate_prompt(prompt: Prompt) -> None:
    """Compile-time-style tripwire. Raises before any provider dispatch."""
    if prompt.redaction_certified is not True:
        raise PromptNotRedacted()
    for key in prompt.user_context.keys():
        if _PII_KEY_REGEX.search(key):
            raise PromptContainsPII(key)


async def _resolve_provider(db: AsyncSession, org_id: int) -> tuple[str, str]:
    """Return ``(provider, model)`` from ``org_settings``.

    PR-B does not read ``org_settings`` yet (the table is reachable but
    every org defaults to ``"mock"`` until ops explicitly flips it).
    Returning the default here keeps the call site shaped correctly so
    PR-C / the real-adapter PR can add the lookup without changing
    ``call_llm``.
    """
    # Reserved for future expansion; PR-B is mock-only.
    _ = (db, org_id)
    return ("mock", "")


async def call_llm(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: FeatureKey,
    prompt: Prompt,
    dry_run: bool = False,
) -> LLMResult:
    """Provider-neutral LLM call.

    PR-B behavior (mock-only):
    1. Validate ``prompt`` (PII keys, redaction certified).
    2. Fail-closed on the feature gate.
    3. Dispatch to ``mock_adapter`` (every call is effectively dry-run).
    4. Emit a single ``ai.call`` structlog event with the whitelisted
       telemetry fields from spec §6 — never prompt content.

    Future PRs add:
    - PR-C: cap-check via ``ai_usage`` aggregation + ``ai_usage`` row
      writes for every outcome (success, gate-closed, over-cap).
    - Real-adapter PR: dispatch to ``anthropic_adapter`` or
      ``openai_adapter`` based on ``org_settings.ai.provider``.

    Raises:
        PromptNotRedacted: ``prompt.redaction_certified is not True``.
        PromptContainsPII: ``prompt.user_context`` has a PII-shaped key.
        FeatureNotEnabled: the org's feature gate for ``feature_key``
            resolves to False.
        ValidationError: ``feature_key`` is not in the catalog.
    """
    if feature_key not in ALL_FEATURE_KEYS:
        # Catalog unknown — caller bug. Same surfacing rule as
        # feature_service.UnknownFeatureKey (HTTP 500), but we raise
        # ValidationError here so it threads through the existing
        # exception mapper. UnknownFeatureKey is the deeper raise
        # path inside feature_service.has_feature() below.
        raise ValidationError(f"Unknown AI feature key: {feature_key!r}")

    _validate_prompt(prompt)

    request_id = uuid.uuid4().hex

    # Defense-in-depth gate check. Even if the router forgot to
    # `Depends(require_feature(...))`, this raises before any adapter
    # work happens. The structlog event records the rejection so the
    # coverage endpoint can see "gate-closed" as a distinct outcome.
    gate_open = await feature_service.has_feature(db, org_id, feature_key)
    if not gate_open:
        await logger.ainfo(
            "ai.call",
            org_id=org_id,
            feature_key=feature_key,
            provider="(none)",
            model="(none)",
            tokens_in=0,
            tokens_out=0,
            cost_cents=0,
            latency_ms=0,
            dry_run=dry_run,
            status=STATUS_REJECTED_GATE_CLOSED,
            error_code=None,
            request_id=request_id,
        )
        raise FeatureNotEnabled(org_id, feature_key)

    provider, model = await _resolve_provider(db, org_id)

    # PR-B is mock-only. ``dry_run`` flag is honored but the mock
    # adapter does not differentiate — every call is deterministic.
    # PR-C's cap-check sits between here and the dispatch.
    if provider == "mock" or dry_run:
        result = mock_adapter.dispatch(prompt=prompt, request_id=request_id)
    else:  # pragma: no cover — unreachable in PR-B; the real-adapter PR adds this branch.
        # Future: dispatch to anthropic_adapter / openai_adapter.
        # Until then, treat as mock so we cannot accidentally leak.
        result = mock_adapter.dispatch(prompt=prompt, request_id=request_id)

    await logger.ainfo(
        "ai.call",
        org_id=org_id,
        feature_key=feature_key,
        provider=result.provider,
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_cents=result.cost_cents,
        latency_ms=result.latency_ms,
        dry_run=result.dry_run,
        status=result.status,
        error_code=None,
        request_id=result.request_id,
    )

    return result
