"""LAI.1 — AI-assisted transaction categorization router.

POST /api/v1/ai/categorize — suggest a category for an existing
transaction. Org-scoped, JWT-authenticated, gated on the
``ai.autocategorize`` feature. The suggestion is advisory: the user
must accept it explicitly on the frontend.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.feature_deps import require_feature
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.ai_categorize import CategorizeRequest, CategorizeSuggestion
from app.services import ai_categorize_service
from app.services.ai_categorize_service import (
    CategoryCatalogEmpty,
    SuggestionRejected,
    TransactionNotFound,
)
from app.services.ai_dispatch import (
    AICapabilityNotSupported,
    AICapExceeded,
    AIDispatchFailed,
    NoRoutingConfigured,
)
from app.services.ai_providers import NativeNotAvailable, StructuredOutputError


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.post(
    "/categorize",
    response_model=CategorizeSuggestion,
    status_code=status.HTTP_200_OK,
)
async def categorize_transaction(
    payload: CategorizeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(get_current_user),
    _features: dict = Depends(require_feature("ai.autocategorize")),
) -> CategorizeSuggestion:
    """Suggest a category for ``payload.transaction_id``.

    Status code mapping:
    - 200: suggestion ready (frontend should pre-fill the dropdown).
    - 403: feature gate closed for the org (``require_feature``).
    - 404: transaction not found / cross-org.
    - 409: org has no categories of the right type (``CategoryCatalogEmpty``).
    - 412: routing not configured, native not available, or
           the routed credential lacks ``structured_output``.
    - 402: hard cap exceeded for the period.
    - 502: provider error or structured-output retry budget exhausted.
    """
    try:
        category, confidence, reasoning = (
            await ai_categorize_service.suggest_category(
                db,
                org_id=current_user.org_id,
                transaction_id=payload.transaction_id,
                session_factory=session_factory,
                actor_user_id=current_user.id,
                actor_email=current_user.email,
                request_id=_request_id(),
                ip_address=get_client_ip(request),
            )
        )
    except TransactionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "transaction_not_found"},
        )
    except CategoryCatalogEmpty:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "category_catalog_empty"},
        )
    except NoRoutingConfigured:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"code": "ai_routing_not_configured"},
        )
    except AICapabilityNotSupported as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "code": exc.code,
                "capability": exc.capability,
                "feature_key": exc.feature_key,
            },
        )
    except NativeNotAvailable:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"code": "ai_native_not_available"},
        )
    except AICapExceeded:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "ai_hard_cap_exceeded"},
        )
    except (StructuredOutputError, SuggestionRejected) as exc:
        # Both surface as a 502: we got an answer from the provider
        # but it wasn't usable. Distinguish the codes so the frontend
        # can decide whether to retry or surface a "model is having
        # trouble" hint.
        code = (
            "ai_structured_output_failed"
            if isinstance(exc, StructuredOutputError)
            else f"suggestion_rejected:{exc.code}"
        )
        logger.info(
            "ai.categorize.failed",
            org_id=current_user.org_id,
            transaction_id=payload.transaction_id,
            code=code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": code},
        )
    except AIDispatchFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code},
        )

    return CategorizeSuggestion(
        transaction_id=payload.transaction_id,
        category_id=category.id,
        category_name=category.name,
        confidence=confidence,
        reasoning=reasoning,
    )
