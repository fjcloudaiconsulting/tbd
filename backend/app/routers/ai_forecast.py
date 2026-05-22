"""LAI.2 — AI-refined Smart Forecast endpoint.

Surface: ``POST /api/v1/ai/forecast/refine``. Opt-in refinement that
layers AI-detected seasonality + anomaly flags on top of the
deterministic forecast. Falls back to the baseline on any LLM failure
so the UI always renders something useful.

Gating:
- ``require_feature("ai.forecast")``, the same feature gate the AI
  tier catalog defines. Closed gate returns 403 ``feature_not_enabled``.
- BYO routing + cap enforcement happen inside
  ``ai_dispatch.call_llm_structured``. Missing routing /
  cap-exceeded surface as "fallback to baseline" with a typed reason
  in ``provenance.fallback_reason``, NOT as a hard error, so the user
  still gets a usable forecast.

Audit: every call writes an ``ai.forecast.refine.invoked`` event with
the outcome (``success`` for AI-applied, ``failure`` for fallback) and
the provenance reason. We deliberately do not log the LLM's free-text
``summary`` because that could echo PII back through the audit log.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.feature_deps import require_feature
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.ai_forecast import (
    RefineForecastRequest,
    RefinedForecastResponse,
)
from app.services import audit_service
from app.services.ai_forecast_refine_service import refine_forecast


logger = structlog.stdlib.get_logger()


router = APIRouter(prefix="/api/v1/ai/forecast", tags=["ai", "forecast"])


@router.post("/refine", response_model=RefinedForecastResponse)
async def refine_forecast_endpoint(
    request: Request,
    body: RefineForecastRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    _gate: dict = Depends(require_feature("ai.forecast")),
):
    period_start_date: Optional[datetime.date] = None
    if body.period_start:
        try:
            period_start_date = datetime.date.fromisoformat(body.period_start)
        except ValueError:
            # Pydantic keeps period_start as a string for shape stability;
            # surface bad input as a 400 rather than letting the service
            # blow up on a malformed ISO date.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_period_start",
                    "message": "period_start must be ISO date YYYY-MM-DD",
                },
            )

    refined = await refine_forecast(
        db, org_id=current_user.org_id, period_start=period_start_date
    )

    # Audit AFTER the business operation. The audit row carries the
    # outcome (was AI applied? was it a fallback?) but never carries
    # the LLM's summary text, because that could echo user-typed
    # category names back through audit storage.
    outcome = "success" if refined.provenance.ai_applied else "failure"
    detail = {
        "ai_applied": refined.provenance.ai_applied,
        "fallback_reason": refined.provenance.fallback_reason,
        "model": refined.provenance.model,
        "categories_adjusted": sum(
            1 for c in refined.categories if c.multiplier != 1.0
        ),
        "anomalies_flagged": len(refined.anomalies),
        "period_start": refined.period_start,
    }
    try:
        await audit_service.record_audit_event(
            session_factory,
            event_type="ai.forecast.refine.invoked",
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=current_user.org_id,
            target_org_name=None,
            request_id=uuid.uuid4().hex,
            ip_address=get_client_ip(request),
            outcome=outcome,
            detail=detail,
        )
    except Exception as exc:  # pragma: no cover - audit failure must not break user flow
        logger.warning(
            "ai.forecast.refine.audit_failed",
            org_id=current_user.org_id,
            error_class=type(exc).__name__,
        )

    return refined
