"""Mock LLM adapter — deterministic canned responses.

Used by:
- Every org whose ``org_settings.ai.provider`` is ``"mock"`` (default).
- Every ``call_llm(..., dry_run=True)`` request, regardless of provider.
- Backend test suites that exercise LAI surfaces.

The "canned response" is a deterministic function of the prompt's
``user_context`` so tests can assert on exact return values without
mocking the adapter directly. Every mock result reports
``cost_cents=0`` and ``dry_run=True`` so the cap-check (PR-C) treats
mock dispatches as free.

This module makes zero network calls. It is safe in every environment.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from app.services.ai_service import LLMResult, Prompt


# Reserved provider/model identifiers for telemetry. The ledger and
# structlog event use these strings verbatim so dashboards can tell
# mock from real-provider rows at a glance.
MOCK_PROVIDER = "mock"
MOCK_MODEL = "mock-1"


def _canned_content(prompt: "Prompt") -> str:
    """Return a deterministic string derived from the prompt.

    Hash inputs:
    - ``system_instructions`` (the prompt template the caller chose).
    - JSON-serialized ``user_context`` (sorted keys for stability).

    The hash is truncated to a short hex prefix so tests can assert
    on the exact content without pinning the full digest.
    """
    payload = json.dumps(
        {
            "sys": prompt.system_instructions,
            "ctx": prompt.user_context,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"[mock:{digest}] canned response"


def dispatch(*, prompt: "Prompt", request_id: str) -> "LLMResult":
    """Return a deterministic LLMResult.

    Counts as ``dry_run=True`` so the PR-C cap-check skips it. No tokens
    consumed, no cost charged, no latency reported.
    """
    # Local import to avoid the import cycle with ai_service. Both modules
    # are stable; the cycle is purely a packaging artefact.
    from app.services.ai_service import LLMResult, STATUS_DRY_RUN

    return LLMResult(
        content=_canned_content(prompt),
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        cost_cents=0,
        dry_run=True,
        provider=MOCK_PROVIDER,
        model=MOCK_MODEL,
        status=STATUS_DRY_RUN,
        request_id=request_id,
    )
