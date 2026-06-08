"""Admin role-management router (L4.8).

Mounted at ``/api/v1/admin/roles``. Gated by the platform
``roles.manage`` permission (superadmin short-circuits today; future
fine-grained roles can hold the permission once the L4.4 user-role
join lands).

Defense in depth: ``is_system_frozen`` is enforced both here and in
the service layer. Mutating endpoints emit a structlog event and
persist the same shape into the L4.7 audit log so an operator can
later attribute who created/edited/deleted which role.

Catalog endpoint ``GET /api/v1/admin/permissions`` is co-located so
the role admin UI doesn't have to hardcode the permission list — it
fetches the live grouped catalog at render time.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from app.auth.permissions import ALL_PERMISSIONS, require_permission
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.role import (
    PermissionCatalogResponse,
    RoleCreate,
    RoleDetailResponse,
    RoleListItem,
    RoleListResponse,
    RoleUpdate,
)
from app.services import audit_service, role_service
from app.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


logger = structlog.stdlib.get_logger()


router = APIRouter(tags=["admin-roles"])


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")


# ── Permissions catalog ──────────────────────────────────────────────────


@router.get(
    "/api/v1/admin/permissions",
    response_model=PermissionCatalogResponse,
    dependencies=[Depends(require_permission("roles.manage"))],
)
async def list_permission_catalog() -> PermissionCatalogResponse:
    grouped = role_service.grouped_permissions()
    return PermissionCatalogResponse(
        namespaces=grouped,
        keys=sorted(ALL_PERMISSIONS),
    )


# ── Role CRUD ────────────────────────────────────────────────────────────


@router.get(
    "/api/v1/admin/roles",
    response_model=RoleListResponse,
    dependencies=[Depends(require_permission("roles.manage"))],
)
async def list_roles(db: AsyncSession = Depends(get_db)) -> RoleListResponse:
    items = await role_service.list_roles(db)
    # Roles are fully listed (no offset/limit), so ``total`` is simply
    # the row count. The envelope now matches the other admin tables so
    # the FE can reuse the shared <Pagination> component.
    return RoleListResponse(
        items=[RoleListItem(**item) for item in items],
        total=len(items),
    )


@router.get(
    "/api/v1/admin/roles/{role_id}",
    response_model=RoleDetailResponse,
    dependencies=[Depends(require_permission("roles.manage"))],
)
async def get_role(
    role_id: int, db: AsyncSession = Depends(get_db)
) -> RoleDetailResponse:
    try:
        item = await role_service.get_role(db, role_id=role_id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    return RoleDetailResponse(**item)


@router.post(
    "/api/v1/admin/roles",
    response_model=RoleDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_role(
    body: RoleCreate,
    request: Request,
    current_user: User = Depends(require_permission("roles.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> RoleDetailResponse:
    try:
        item = await role_service.create_role(
            db,
            slug=body.slug,
            name=body.name,
            description=body.description,
            permissions=body.permissions,
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )
    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        )
    await db.commit()

    await logger.ainfo(
        "admin.role.created",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        role_id=item["id"],
        role_slug=item["slug"],
        permission_count=len(item["permissions"]),
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.role.created",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "role_id": item["id"],
            "role_slug": item["slug"],
            "role_name": item["name"],
            "permission_count": len(item["permissions"]),
        },
    )
    return RoleDetailResponse(**item)


@router.patch(
    "/api/v1/admin/roles/{role_id}",
    response_model=RoleDetailResponse,
)
async def update_role(
    role_id: int,
    body: RoleUpdate,
    request: Request,
    current_user: User = Depends(require_permission("roles.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> RoleDetailResponse:
    # Defense-in-depth frozen guard: fetch once for the 404 + frozen
    # check, then let the service patch in the same session.
    try:
        existing = await role_service.get_role(db, role_id=role_id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    if existing["is_system_frozen"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Role {existing['slug']!r} is a frozen system role "
                "and cannot be edited"
            ),
        )

    # Distinguish "description omitted" from "description explicitly null"
    # so the user can clear the field by sending `{"description": null}`.
    # Pydantic's model_fields_set tracks which keys were present in the
    # request body even when their value is None (PR #142 #3).
    description_provided = "description" in body.model_fields_set
    try:
        item = await role_service.update_role(
            db,
            role_id=role_id,
            name=body.name,
            description=body.description,
            clear_description=description_provided and body.description is None,
            permissions=body.permissions,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )
    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        )
    await db.commit()

    # Treat any field present in the request body as "changed" — including
    # `description: null` (clear). Without model_fields_set this would miss
    # the explicit-clear case.
    changed_fields = [
        f
        for f in ("name", "description", "permissions")
        if f in body.model_fields_set
    ]
    # Capture the permission delta for the audit row. Keys are not
    # sensitive (the catalog is exposed via /admin/permissions to any
    # caller with roles.manage), so logging the actual sets gives an
    # operator the accountability trail we want without leaking
    # anything.
    before_perms = sorted(existing["permissions"])
    after_perms = sorted(item["permissions"])
    perm_added = sorted(set(after_perms) - set(before_perms))
    perm_removed = sorted(set(before_perms) - set(after_perms))

    await logger.ainfo(
        "admin.role.updated",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        role_id=item["id"],
        role_slug=item["slug"],
        changed_fields=changed_fields,
        permissions_added=perm_added,
        permissions_removed=perm_removed,
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.role.updated",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "role_id": item["id"],
            "role_slug": item["slug"],
            "changed_fields": changed_fields,
            "permission_count": len(item["permissions"]),
            "permissions_added": perm_added,
            "permissions_removed": perm_removed,
        },
    )
    return RoleDetailResponse(**item)


@router.delete(
    "/api/v1/admin/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_role(
    role_id: int,
    request: Request,
    current_user: User = Depends(require_permission("roles.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> Response:
    try:
        existing = await role_service.get_role(db, role_id=role_id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    # Router-side frozen guard. Service enforces too.
    if existing["is_system_frozen"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Role {existing['slug']!r} is a frozen system role "
                "and cannot be deleted"
            ),
        )

    try:
        await role_service.delete_role(db, role_id=role_id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        )
    await db.commit()

    await logger.ainfo(
        "admin.role.deleted",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        role_id=role_id,
        role_slug=existing["slug"],
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.role.deleted",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "role_id": role_id,
            "role_slug": existing["slug"],
            "role_name": existing["name"],
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
