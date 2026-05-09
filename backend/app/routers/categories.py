from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.database import get_db
from app.deps import get_current_user
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.category import (
    BatchMoveRequest,
    BatchMoveResult,
    CategoryCreate,
    CategoryDeleteResult,
    CategoryMoveRequest,
    CategoryMoveResult,
    CategoryResponse,
    CategoryUpdate,
)
from app.services import audit_service, category_service
from app.services.category_service import (
    batch_move_subcategories,
    delete_category_with_migration,
    move_subcategory,
    preview_move,
    validate_category_type_change,
)
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])

logger = structlog.stdlib.get_logger()

Parent = aliased(Category)


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")


async def _actor_org_name(db: AsyncSession, org_id: int) -> str:
    """Snapshot the org name for audit rows."""
    from app.models.user import Organization

    name = await db.scalar(
        select(Organization.name).where(Organization.id == org_id)
    )
    return name or ""


def _to_response(cat: Category, parent_name: str | None, tx_count: int) -> CategoryResponse:
    return CategoryResponse(
        id=cat.id,
        name=cat.name,
        type=cat.type.value,
        parent_id=cat.parent_id,
        parent_name=parent_name,
        description=cat.description,
        slug=cat.slug,
        is_system=cat.is_system,
        transaction_count=tx_count,
    )


# ── Domain-error mapping helpers ─────────────────────────────────────────────


def _name_collision_response(detail_str: str) -> HTTPException:
    """Parse the ``name_collision`` ConflictError encoding and build the
    structured 409 response."""
    parts = detail_str.split("::", 4)
    # parts: ["name_collision", target_id, conflicting_id, name, normalized]
    try:
        target_parent_id = int(parts[1])
        conflicting_child_id = int(parts[2])
        conflicting_child_name = parts[3]
        normalized_name = parts[4]
    except (IndexError, ValueError):
        return HTTPException(status_code=409, detail={"detail": "name_collision"})
    return HTTPException(
        status_code=409,
        detail={
            "detail": "name_collision",
            "target_parent_id": target_parent_id,
            "conflicting_child_id": conflicting_child_id,
            "conflicting_child_name": conflicting_child_name,
            "normalized_name": normalized_name,
        },
    )


def _has_children_response(detail_str: str) -> HTTPException:
    """Parse the ``has_children`` ConflictError encoding."""
    parts = detail_str.split("::", 1)
    child_ids: list[int] = []
    child_names: list[str] = []
    if len(parts) >= 2:
        for entry in parts[1].split(":"):
            if "|" not in entry:
                continue
            cid_str, _, name = entry.partition("|")
            try:
                child_ids.append(int(cid_str))
            except ValueError:
                continue
            child_names.append(name)
    return HTTPException(
        status_code=409,
        detail={
            "detail": "has_children",
            "child_ids": child_ids,
            "child_names": child_names,
        },
    )


def _type_mismatch_response(detail_str: str) -> HTTPException:
    """Parse the ``type_mismatch::source_type::target_type::income::expense`` encoding."""
    parts = detail_str.split("::", 4)
    try:
        source_type = parts[1]
        target_type = parts[2]
        income_count = int(parts[3])
        expense_count = int(parts[4])
    except (IndexError, ValueError):
        return HTTPException(
            status_code=400, detail={"detail": "type_mismatch"},
        )
    return HTTPException(
        status_code=400,
        detail={
            "detail": "type_mismatch",
            "source_type": source_type,
            "target_type": target_type,
            "dependent_breakdown": {
                "income": income_count,
                "expense": expense_count,
            },
        },
    )


def _migration_target_required_response(detail_str: str) -> HTTPException:
    parts = detail_str.split("::", 3)
    try:
        tx_count = int(parts[1])
        rec_count = int(parts[2])
        fpi_count = int(parts[3])
    except (IndexError, ValueError):
        return HTTPException(
            status_code=422, detail={"detail": "migration_target_required"},
        )
    return HTTPException(
        status_code=422,
        detail={
            "detail": "migration_target_required",
            "dependent_counts": {
                "transactions": tx_count,
                "recurring": rec_count,
                "forecast_items": fpi_count,
            },
        },
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            Category,
            Parent.name.label("parent_name"),
            func.count(Transaction.id).label("transaction_count"),
        )
        .outerjoin(Parent, Category.parent_id == Parent.id)
        .outerjoin(
            Transaction,
            (Transaction.category_id == Category.id)
            & (Transaction.org_id == current_user.org_id),
        )
        .where(Category.org_id == current_user.org_id)
        .group_by(Category.id, Parent.name)
        .order_by(Category.parent_id.is_(None).desc(), Parent.name, Category.name)
    )
    return [
        _to_response(cat, parent_name, count)
        for cat, parent_name, count in result.all()
    ]


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    request: Request,
    body: CategoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Children silently inherit their parent's type.
    effective_type = CategoryType(body.type)
    if body.parent_id is not None:
        parent_row = await db.execute(
            select(Category).where(
                Category.id == body.parent_id, Category.org_id == current_user.org_id
            )
        )
        parent_cat = parent_row.scalar_one_or_none()
        if parent_cat is None:
            raise HTTPException(status_code=400, detail="Invalid parent category")
        if parent_cat.parent_id is not None:
            raise HTTPException(status_code=400, detail="Cannot nest more than two levels")
        effective_type = parent_cat.type

    cat = Category(
        org_id=current_user.org_id,
        name=body.name,
        type=effective_type,
        parent_id=body.parent_id,
        description=body.description,
    )
    db.add(cat)
    await db.flush()

    # Audit row staged in same txn (excluded for org-bootstrap-seed,
    # which never goes through this router).
    org_name = await _actor_org_name(db, current_user.org_id)
    audit_service.add_audit_event_to_session(
        db,
        event_type="category.created",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "category_id": cat.id,
            "name": cat.name,
            "type": cat.type.value,
            "parent_id": cat.parent_id,
            "is_system": False,
        },
    )
    await logger.ainfo(
        "category.created",
        category_id=cat.id,
        type=cat.type.value,
        parent_id=cat.parent_id,
    )

    await db.commit()
    await db.refresh(cat)

    parent_name = None
    if cat.parent_id:
        parent_name = await db.scalar(
            select(Category.name).where(
                Category.id == cat.parent_id, Category.org_id == current_user.org_id
            )
        )

    return _to_response(cat, parent_name, 0)


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(
    request: Request,
    category_id: int,
    body: CategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Category).where(
            Category.id == category_id, Category.org_id == current_user.org_id
        )
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=404, detail="Category not found")

    # Subcategory invariant: child.type must equal parent.type.
    if (
        cat.parent_id is not None
        and body.type is not None
        and CategoryType(body.type) != cat.type
    ):
        parent_type = await db.scalar(
            select(Category.type).where(
                Category.id == cat.parent_id,
                Category.org_id == current_user.org_id,
            )
        )
        if parent_type is None or CategoryType(body.type) != parent_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Subcategory type must match its parent. Update the "
                    "master category instead."
                ),
            )

    old_name = cat.name
    old_type = cat.type
    rename_changed = False
    type_changed = False
    cascaded_child_ids: list[int] = []

    if body.name is not None and body.name != cat.name:
        cat.name = body.name
        rename_changed = True

    if body.type is not None:
        new_type = CategoryType(body.type)
        if new_type != cat.type:
            try:
                await validate_category_type_change(db, cat, new_type)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.detail) from exc

            # Floor invariant guard (Invariant 1, cross-ref Invariant 4):
            # reject if the type change would drop the org below the
            # 1+1+1+1 floor for masters or subcategories of the OLD type.
            try:
                await category_service.assert_min_floor_after_type_change(
                    db,
                    org_id=current_user.org_id,
                    category=cat,
                    new_type=new_type,
                )
            except ConflictError as exc:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=category_service._parse_floor_violation_detail(
                        exc.detail
                    ),
                ) from exc

            # Codebase invariant: child type == master type.
            if cat.parent_id is None:
                children = (await db.scalars(
                    select(Category).where(
                        Category.parent_id == cat.id,
                        Category.org_id == current_user.org_id,
                    )
                )).all()
                for child in children:
                    child.type = new_type
                    cascaded_child_ids.append(child.id)

            cat.type = new_type
            type_changed = True

    if body.description is not None:
        cat.description = body.description

    # Audit rows staged before commit (one per logical change). Only
    # emit when a value actually changed (per §D footer convention).
    org_name = await _actor_org_name(db, current_user.org_id)
    if rename_changed:
        audit_service.add_audit_event_to_session(
            db,
            event_type="category.renamed",
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=current_user.org_id,
            target_org_name=org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={
                "category_id": cat.id,
                "old_name": old_name,
                "new_name": cat.name,
                "parent_id": cat.parent_id,
                "type": cat.type.value,
            },
        )
        await logger.ainfo(
            "category.renamed",
            category_id=cat.id,
            old_name=old_name,
            new_name=cat.name,
        )
    if type_changed:
        audit_service.add_audit_event_to_session(
            db,
            event_type="category.type_changed",
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=current_user.org_id,
            target_org_name=org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={
                "category_id": cat.id,
                "old_type": old_type.value,
                "new_type": cat.type.value,
                "is_master": cat.parent_id is None,
                "child_ids_cascaded": cascaded_child_ids,
            },
        )
        await logger.ainfo(
            "category.type_changed",
            category_id=cat.id,
            old_type=old_type.value,
            new_type=cat.type.value,
        )

    await db.commit()
    await db.refresh(cat)

    parent_name = None
    if cat.parent_id:
        parent_name = await db.scalar(
            select(Category.name).where(
                Category.id == cat.parent_id, Category.org_id == current_user.org_id
            )
        )

    count_result = await db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.category_id == cat.id,
            Transaction.org_id == current_user.org_id,
        )
    )
    return _to_response(cat, parent_name, count_result or 0)


# ── C0: move preview / move / batch-move / delete-with-migration ────────────


@router.get(
    "/{category_id}/move/preview",
    response_model=CategoryMoveResult,
)
async def preview_move_endpoint(
    category_id: int,
    target_parent_id: int = Query(..., description="ID of the proposed new master"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read-only move preview. Issues SELECTs only; no audit row, no
    structlog event, no writes (§4.1 of the C0 spec)."""
    try:
        return await preview_move(
            db,
            org_id=current_user.org_id,
            subcategory_id=category_id,
            target_parent_id=target_parent_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        if exc.detail.startswith("name_collision::"):
            raise _name_collision_response(exc.detail) from exc
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@router.patch("/{category_id}/move", response_model=CategoryMoveResult)
async def move_category_endpoint(
    request: Request,
    category_id: int,
    body: CategoryMoveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Move a single subcategory under a different master."""
    org_name = await _actor_org_name(db, current_user.org_id)
    try:
        result = await move_subcategory(
            db,
            org_id=current_user.org_id,
            subcategory_id=category_id,
            target_parent_id=body.target_parent_id,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            actor_org_name=org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except NotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        await db.rollback()
        if exc.detail.startswith("name_collision::"):
            raise _name_collision_response(exc.detail) from exc
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValidationError as exc:
        await db.rollback()
        if exc.detail.startswith("type_mismatch"):
            raise HTTPException(
                status_code=400,
                detail={"detail": "type_mismatch", "message": exc.detail},
            ) from exc
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    await db.commit()
    return result


@router.post("/batch-move", response_model=BatchMoveResult)
async def batch_move_endpoint(
    request: Request,
    body: BatchMoveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomic batch move of N subcategories."""
    org_name = await _actor_org_name(db, current_user.org_id)
    try:
        return await batch_move_subcategories(
            db,
            org_id=current_user.org_id,
            moves=list(body.moves),
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            actor_org_name=org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        if exc.detail.startswith("name_collision::"):
            raise _name_collision_response(exc.detail) from exc
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValidationError as exc:
        if exc.detail.startswith("type_mismatch"):
            raise HTTPException(
                status_code=400,
                detail={"detail": "type_mismatch", "message": exc.detail},
            ) from exc
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@router.delete("/{category_id}")
async def delete_category(
    request: Request,
    category_id: int,
    target_category_id: Optional[int] = Query(
        None, description="Migration target ID (required when category has dependents)",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a category, optionally migrating dependents to a target.

    - 200 with ``CategoryDeleteResult`` body: dependents migrated.
    - 204 (no body): no dependents, nothing to migrate.
    - 409 ``has_children``: master has child subcategories.
    - 409 ``last_in_type``: would drop org below 1+1 floor.
    - 422 ``migration_target_required``: dependents present but target missing.
    - 400 ``type_mismatch``: target type incompatible with source dependents.
    """
    org_name = await _actor_org_name(db, current_user.org_id)
    try:
        result, had_dependents = await delete_category_with_migration(
            db,
            org_id=current_user.org_id,
            category_id=category_id,
            target_category_id=target_category_id,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            actor_org_name=org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except NotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        await db.rollback()
        if exc.detail.startswith("has_children::") or exc.detail == "has_children":
            raise _has_children_response(exc.detail) from exc
        if exc.detail == "last_in_type":
            # Re-fetch the category to compute the structured detail.
            cat = await db.scalar(
                select(Category).where(
                    Category.id == category_id,
                    Category.org_id == current_user.org_id,
                )
            )
            if cat is not None:
                detail = await category_service._floor_conflict_detail(
                    db, org_id=current_user.org_id, category=cat,
                )
                raise HTTPException(status_code=409, detail=detail) from exc
            raise HTTPException(
                status_code=409, detail={"detail": "last_in_type"},
            ) from exc
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValidationError as exc:
        await db.rollback()
        if exc.detail.startswith("migration_target_required::"):
            raise _migration_target_required_response(exc.detail) from exc
        if exc.detail.startswith("type_mismatch::"):
            raise _type_mismatch_response(exc.detail) from exc
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    await db.commit()
    if had_dependents:
        return result
    # 204 No Content for the no-dependents path.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
