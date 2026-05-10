"""Tags router (PR-Tags-A).

Mounted at ``/api/v1/tags``. Thin delegate to ``app.services.tag_service``
matching the project's existing router/service pattern.

Audit events written here:

- ``tag.created`` for POST /api/v1/tags
- ``tag.renamed`` for PATCH /api/v1/tags/{id}
- ``tag.deleted`` for DELETE /api/v1/tags/{id}
- ``transaction.tags.replaced`` for PUT /api/v1/transactions/{id}/tags

The merge endpoint and ``tag.merged`` audit event are out of scope for
PR-Tags-A (the spec lists merge under PR-Tags-C as a management UI
feature; we leave the service hook unimplemented and the audit name
documented only).
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.tag import (
    TagCreate,
    TagRename,
    TagResponse,
    TagSuggestionsResponse,
    TransactionTagSetReplace,
)
from app.services import audit_service, tag_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])

# Transaction-side endpoint for replacing the tag set lives at a path
# under /api/v1/transactions but is collocated here so the entire tag
# surface is in one file. Wired into the app at main.py via this same
# router (FastAPI routes by path, not file).
transaction_tags_router = APIRouter(
    prefix="/api/v1/transactions", tags=["tags"]
)


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.get("", response_model=list[TagResponse])
async def list_tags(
    q: Optional[str] = Query(default=None, max_length=64),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the org's tags with per-tag usage counts."""
    rows = await tag_service.list_org_tags(db, current_user.org_id, q=q)
    return [
        TagResponse(
            id=tag.id,
            name=tag.name,
            name_normalized=tag.name_normalized,
            usage_count=usage,
        )
        for tag, usage in rows
    ]


@router.post("", response_model=TagResponse, status_code=201)
async def create_tag_endpoint(
    body: TagCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Create a new org-local tag."""
    try:
        tag = await tag_service.create_tag(
            db,
            org_id=current_user.org_id,
            name=body.name,
            created_by_user_id=current_user.id,
        )
        await db.commit()
        await db.refresh(tag)
    except ValidationError:
        await db.rollback()
        raise
    except ConflictError:
        await db.rollback()
        raise

    await audit_service.record_audit_event(
        session_factory,
        event_type="tag.created",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"tag_id": tag.id, "name": tag.name_normalized},
    )

    return TagResponse(
        id=tag.id,
        name=tag.name,
        name_normalized=tag.name_normalized,
        usage_count=0,
    )


@router.patch("/{tag_id}", response_model=TagResponse)
async def rename_tag_endpoint(
    tag_id: int,
    body: TagRename,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Rename an existing tag in place."""
    try:
        tag = await tag_service.rename_tag(
            db,
            org_id=current_user.org_id,
            tag_id=tag_id,
            new_name=body.name,
        )
        old_normalized = tag.name_normalized  # already updated by the service
        await db.commit()
        await db.refresh(tag)
    except NotFoundError:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Tag not found")
    except (ValidationError, ConflictError):
        await db.rollback()
        raise

    await audit_service.record_audit_event(
        session_factory,
        event_type="tag.renamed",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"tag_id": tag.id, "name": tag.name_normalized},
    )
    return TagResponse(
        id=tag.id,
        name=tag.name,
        name_normalized=tag.name_normalized,
        usage_count=0,
    )


@router.delete("/{tag_id}", status_code=204)
async def delete_tag_endpoint(
    tag_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Delete a tag. Cascade detaches it from every transaction."""
    try:
        tag = await tag_service.delete_tag(
            db,
            org_id=current_user.org_id,
            tag_id=tag_id,
        )
        deleted_name = tag.name_normalized
        await db.commit()
    except NotFoundError:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Tag not found")

    await audit_service.record_audit_event(
        session_factory,
        event_type="tag.deleted",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"tag_id": tag_id, "name": deleted_name},
    )
    return None


@router.get("/suggest", response_model=TagSuggestionsResponse)
async def suggest_endpoint(
    prefix: Optional[str] = Query(default=None, alias="prefix", max_length=32),
    category_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=10, ge=1, le=25),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Three-pass autocomplete (org_co_category -> org_recent -> shared_dictionary).

    The shared_dictionary pass is skipped unless the org has
    ``share_tag_data=true`` AND each candidate tag passes the
    k-anonymity floor (or is a seed). See ``tag_service.suggest_tags``.
    """
    suggestions = await tag_service.suggest_tags(
        db,
        org_id=current_user.org_id,
        prefix=prefix,
        category_id=category_id,
        limit=limit,
    )
    return TagSuggestionsResponse(suggestions=suggestions)


@transaction_tags_router.put(
    "/{transaction_id}/tags",
    response_model=list[TagResponse],
)
async def replace_transaction_tags_endpoint(
    transaction_id: int,
    body: TransactionTagSetReplace,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Replace the full tag set on a transaction.

    Cap of 5 enforced at the schema (Field max_length) and the service
    (defense in depth). Tag names are auto-created if they don't exist
    yet in the org.
    """
    try:
        tags = await tag_service.set_transaction_tags(
            db,
            org_id=current_user.org_id,
            transaction_id=transaction_id,
            tag_names=body.tag_names,
            created_by_user_id=current_user.id,
        )
        await db.commit()
    except NotFoundError:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Transaction not found")
    except (ValidationError, ConflictError):
        await db.rollback()
        raise

    await audit_service.record_audit_event(
        session_factory,
        event_type="transaction.tags.replaced",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "transaction_id": transaction_id,
            "tag_names": [t.name_normalized for t in tags],
        },
    )
    return [
        TagResponse(
            id=t.id,
            name=t.name,
            name_normalized=t.name_normalized,
            usage_count=0,
        )
        for t in tags
    ]
