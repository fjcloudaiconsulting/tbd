"""LAI.3 — Smart Budget Rebalance router.

Endpoint: ``POST /api/v1/ai/budget/rebalance``.

Asks the LLM for category-level budget deltas via
``budget_rebalance_service.suggest_rebalance``. The response is a
SUGGESTION only — the frontend collects user accept/skip per row and
re-uses the existing ``PUT /api/v1/budgets/{id}`` endpoints to apply
the change.

Feature gate: ``ai.budget`` (user-facing 403). The dispatch substrate
also re-checks routing + caps internally; LLM failures are surfaced as
``status=llm_unavailable`` (HTTP 200) so the UI shows an empty state
instead of crashing.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.feature_deps import require_feature
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.budget_rebalance import BudgetRebalanceResponse
from app.services import audit_service, budget_rebalance_service


logger = structlog.stdlib.get_logger()


router = APIRouter(prefix="/api/v1/ai/budget", tags=["ai-budget"])


@router.post(
    "/rebalance",
    response_model=BudgetRebalanceResponse,
    dependencies=[Depends(require_feature("ai.budget"))],
)
async def rebalance(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
) -> BudgetRebalanceResponse:
    """Ask the AI for budget-rebalance suggestions for the current period.

    Always returns 200 with a typed ``status``:
      - ``ok`` + non-empty suggestions: render the diff modal.
      - ``ok`` + empty suggestions: LLM declined to suggest changes.
      - ``empty_no_budgets``: org has no current-period budgets.
      - ``empty_no_history``: no settled expense history in last 3mo.
      - ``llm_unavailable``: routing missing, cap exceeded, dispatch
        failed, or structured-output retry budget exhausted.

    Audit-logged with the structural outcome + suggestion count only;
    no prompt / completion content, no category names.
    """
    response = await budget_rebalance_service.suggest_rebalance(
        db,
        org_id=current_user.org_id,
        session_factory=session_factory,
    )

    # Audit outcome distinguishes system failures from user-state
    # preconditions. ``empty_no_budgets`` and ``empty_no_history`` are
    # clean preconditions (the user just hasn't set up budgets / has
    # no recent expenses) — they shouldn't show up as failures in the
    # ops dashboard. Only ``llm_unavailable`` is a real failure.
    audit_outcome = (
        "failure" if response.status == "llm_unavailable" else "success"
    )

    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.budget.rebalance.requested",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        ip_address=get_client_ip(request),
        outcome=audit_outcome,
        detail={
            "status": response.status,
            "suggestion_count": len(response.suggestions),
        },
    )

    return response
