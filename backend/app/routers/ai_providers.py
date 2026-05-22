"""AI provider credentials router (PR1 of the AI tier train).

Mounted at ``/api/v1/settings/ai-providers``. Org-admin gating via
``require_org_admin``; cross-org isolation is enforced by querying
through ``get_credential_for_org(org_id=current_user.organization_id)``
on every read/write path.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.org_permissions import require_org_admin
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.org_ai_credential import (
    OrgAICredentialCreate,
    OrgAICredentialResponse,
    OrgAICredentialRotate,
    OrgAICredentialUpdate,
)
from app.services import ai_credential_service


logger = structlog.stdlib.get_logger()

router = APIRouter(
    prefix="/api/v1/settings/ai-providers",
    tags=["ai-providers"],
)


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.get("", response_model=list[OrgAICredentialResponse])
async def list_credentials(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> list[OrgAICredentialResponse]:
    rows = await ai_credential_service.list_credentials_for_org(
        db, org_id=current_user.organization_id
    )
    return [OrgAICredentialResponse.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=OrgAICredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential(
    payload: OrgAICredentialCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.create_credential(
        db,
        org_id=current_user.organization_id,
        payload=payload,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(row)


@router.get("/{credential_id}", response_model=OrgAICredentialResponse)
async def get_credential(
    credential_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.organization_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return OrgAICredentialResponse.model_validate(row)


@router.patch("/{credential_id}", response_model=OrgAICredentialResponse)
async def update_credential(
    credential_id: int,
    payload: OrgAICredentialUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.organization_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    updated = await ai_credential_service.update_credential_label(
        db,
        credential=row,
        label=payload.label,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(updated)


@router.post(
    "/{credential_id}/rotate", response_model=OrgAICredentialResponse
)
async def rotate_credential(
    credential_id: int,
    payload: OrgAICredentialRotate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.organization_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    updated = await ai_credential_service.rotate_credential(
        db,
        credential=row,
        new_api_key=payload.api_key,
        new_bearer_token=payload.bearer_token,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(updated)


@router.post(
    "/{credential_id}/validate", response_model=OrgAICredentialResponse
)
async def validate_credential(
    credential_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.organization_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    updated = await ai_credential_service.validate_credential(
        db,
        credential=row,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(updated)


@router.delete(
    "/{credential_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_credential(
    credential_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> None:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.organization_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await ai_credential_service.delete_credential(
        db,
        credential=row,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return None
