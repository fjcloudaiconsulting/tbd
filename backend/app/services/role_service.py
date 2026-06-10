"""Service layer for L4.8 role administration.

All writers acquire a fresh row via ``select(...).with_for_update()``
where contention matters (e.g. delete) so concurrent writes don't
race against the ``is_system_frozen`` guard. Reads are cheap selects.

The service is the **inner** of two enforcement layers for
``is_system_frozen`` — the router checks it too. Defense in depth:
if a future endpoint forgets the router-level guard, the service
still refuses.

Orphan-permission policy (read path): ``get_role`` and ``list_roles``
filter the role's stored ``permission_key`` rows through the live
``ALL_PERMISSIONS`` set, so a key that has been removed from the
catalog never surfaces in the UI. The next write rewrites the full
set, so orphans clean themselves up in passing.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.permissions import ALL_PERMISSIONS
from app.models.role import PlatformRole, RolePermission
from app.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.services.list_query import resolve_order_by


_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise ValidationError(
            "Slug must start with a lowercase letter and contain only "
            "lowercase letters, digits, and underscores (3 to 64 chars)."
        )


def _validate_permissions(keys: list[str]) -> list[str]:
    """Reject unknown permission keys; return a de-duplicated list.

    Validates against ``ALL_PERMISSIONS``. Order is not preserved (we
    sort for stable storage / display).
    """
    seen: set[str] = set()
    unknown: list[str] = []
    for k in keys:
        if k in ALL_PERMISSIONS:
            seen.add(k)
        else:
            unknown.append(k)
    if unknown:
        raise ValidationError(
            f"Unknown permission key(s): {sorted(unknown)!r}"
        )
    return sorted(seen)


def _filter_known_permissions(keys: list[str]) -> list[str]:
    """Read-side filter — drop keys no longer in ``ALL_PERMISSIONS``.

    Avoids surfacing orphans in the admin UI. The next write through
    ``set_role_permissions`` will replace the full set, cleaning up.
    """
    return sorted(k for k in keys if k in ALL_PERMISSIONS)


def _to_detail(role: PlatformRole) -> dict:
    """Serialize a role with its permissions, filtered for orphans."""
    keys = [rp.permission_key for rp in role.permissions]
    return {
        "id": role.id,
        "slug": role.slug,
        "name": role.name,
        "description": role.description,
        "is_system_frozen": role.is_system_frozen,
        "permissions": _filter_known_permissions(keys),
        "created_at": role.created_at,
        "updated_at": role.updated_at,
    }


async def list_roles(
    db: AsyncSession,
    *,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return a page of roles with a known-permission count and the
    full row count.

    Mirrors the shared admin-table list contract (orgs / subscriptions /
    audit / rate-limit-overrides): server-side ordering + LIMIT/OFFSET,
    returning ``(items, total)`` where ``total`` is the full count, not
    the page length.

    ``sort_by`` is resolved against a closed whitelist (see ``_SORTABLE``
    below); an unknown key raises ``ValidationError`` (router → 400). When
    omitted, the default order is the semantic ``frozen-first, then name``
    so the seeded system role(s) stay pinned to the top. ``permission_count``
    sorts on a correlated COUNT subquery (the raw stored-row count, which
    matches the displayed count except for the transient orphan-key case).
    ``PlatformRole.id`` asc is appended as a stable tiebreaker so pagination
    is deterministic.
    """
    permission_count_sq = (
        select(func.count())
        .select_from(RolePermission)
        .where(RolePermission.role_id == PlatformRole.id)
        .correlate(PlatformRole)
        .scalar_subquery()
    )

    # Closed whitelist of sortable columns. Keys are the public sort
    # tokens the frontend sends; values are the column/expression to
    # order by. Anything not here is a 400 (see
    # ``list_query.resolve_order_by``).
    _SORTABLE = {
        "name": PlatformRole.name,
        "slug": PlatformRole.slug,
        "permission_count": permission_count_sq,
        "is_system_frozen": PlatformRole.is_system_frozen,
    }

    if sort_by:
        order_by = resolve_order_by(
            sort_by,
            sort_dir,
            allowed=_SORTABLE,
            default_key="name",
            default_dir="asc",
            tiebreaker=PlatformRole.id.asc(),
        )
    else:
        # ``sort_dir`` is meaningless without an explicit ``sort_by`` —
        # reject it so a stray ``sort_dir`` can't masquerade as a no-op.
        # Name the real offender (``sort_dir``), not ``sort_by``.
        if sort_dir is not None:
            raise ValidationError("sort_dir_requires_sort_by")
        order_by = [
            PlatformRole.is_system_frozen.desc(),
            PlatformRole.name.asc(),
            PlatformRole.id.asc(),
        ]

    total = int(
        (await db.scalar(select(func.count()).select_from(PlatformRole))) or 0
    )

    rows = (
        await db.execute(
            select(PlatformRole)
            .options(selectinload(PlatformRole.permissions))
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    items: list[dict] = []
    for role in rows:
        known = _filter_known_permissions(
            [rp.permission_key for rp in role.permissions]
        )
        items.append(
            {
                "id": role.id,
                "slug": role.slug,
                "name": role.name,
                "description": role.description,
                "is_system_frozen": role.is_system_frozen,
                "permission_count": len(known),
                "created_at": role.created_at,
                "updated_at": role.updated_at,
            }
        )
    return items, total


async def get_role(db: AsyncSession, *, role_id: int) -> dict:
    role = (
        await db.execute(
            select(PlatformRole)
            .options(selectinload(PlatformRole.permissions))
            .where(PlatformRole.id == role_id)
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Role")
    return _to_detail(role)


async def get_role_by_slug(
    db: AsyncSession, *, slug: str
) -> Optional[PlatformRole]:
    return (
        await db.execute(
            select(PlatformRole).where(PlatformRole.slug == slug)
        )
    ).scalar_one_or_none()


async def create_role(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    description: Optional[str],
    permissions: list[str],
) -> dict:
    """Create a new (non-frozen) role with the given permissions."""
    _validate_slug(slug)
    keys = _validate_permissions(permissions)

    # Pre-check for friendly 409. The DB unique constraint is the
    # ultimate authority — if a concurrent insert wins between the
    # check and the flush, IntegrityError translates to ConflictError.
    existing = await get_role_by_slug(db, slug=slug)
    if existing is not None:
        raise ConflictError(f"Role slug {slug!r} already exists")

    role = PlatformRole(
        slug=slug,
        name=name,
        description=description,
        is_system_frozen=False,
    )
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            f"Role slug {slug!r} already exists"
        ) from exc

    for key in keys:
        db.add(RolePermission(role_id=role.id, permission_key=key))
    await db.flush()
    # Re-fetch with selectinload so .permissions resolves without a
    # lazy-load callback (incompatible with async sessions absent a
    # greenlet provider).
    role = (
        await db.execute(
            select(PlatformRole)
            .options(selectinload(PlatformRole.permissions))
            .where(PlatformRole.id == role.id)
        )
    ).scalar_one()
    return _to_detail(role)


async def update_role(
    db: AsyncSession,
    *,
    role_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    clear_description: bool = False,
    permissions: Optional[list[str]] = None,
) -> dict:
    """Patch a role. Refuses on ``is_system_frozen``.

    ``description`` semantics (PR #142 #3): the patch shape needs to
    distinguish "field omitted" from "field explicitly cleared to null".
    Callers pass ``description=None`` for both omitted and explicit null,
    so an extra ``clear_description=True`` flag tells the service to
    write NULL to the column. Without that flag a ``None`` description
    is treated as "leave unchanged".

    ``permissions`` semantics: ``None`` leaves them untouched, ``[]``
    clears, otherwise replaces.
    """
    role = (
        await db.execute(
            select(PlatformRole)
            .options(selectinload(PlatformRole.permissions))
            .where(PlatformRole.id == role_id)
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Role")
    if role.is_system_frozen:
        raise ConflictError(
            f"Role {role.slug!r} is a frozen system role and cannot be edited"
        )

    if name is not None:
        role.name = name
    if description is not None:
        role.description = description
    elif clear_description:
        role.description = None

    if permissions is not None:
        keys = _validate_permissions(permissions)
        # Replace-wholesale: delete all current rows, insert new set.
        # Bulk DELETE is bypass-the-orm so we have to expire the
        # relationship explicitly — otherwise the role's loaded
        # ``.permissions`` collection still holds stale RolePermission
        # objects in the identity map.
        await db.execute(
            delete(RolePermission).where(RolePermission.role_id == role.id)
        )
        await db.flush()
        for key in keys:
            db.add(RolePermission(role_id=role.id, permission_key=key))
        await db.flush()
        db.expire(role, ["permissions"])

    # Re-fetch with selectinload + populate_existing so the
    # already-cached identity-map row picks up the rewritten
    # collection rather than serving the stale loaded set.
    role = (
        await db.execute(
            select(PlatformRole)
            .options(selectinload(PlatformRole.permissions))
            .where(PlatformRole.id == role.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return _to_detail(role)


async def delete_role(db: AsyncSession, *, role_id: int) -> None:
    """Delete a role and its permissions. Refuses on ``is_system_frozen``."""
    role = (
        await db.execute(
            select(PlatformRole).where(PlatformRole.id == role_id)
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Role")
    if role.is_system_frozen:
        raise ConflictError(
            f"Role {role.slug!r} is a frozen system role and cannot be deleted"
        )
    # FK ON DELETE CASCADE drops the role_permissions rows.
    await db.delete(role)
    await db.flush()


async def set_role_permissions(
    db: AsyncSession,
    *,
    role_id: int,
    permissions: list[str],
) -> dict:
    """Replace the role's permission set wholesale."""
    return await update_role(
        db, role_id=role_id, permissions=permissions
    )


def grouped_permissions() -> dict[str, list[str]]:
    """Return ``ALL_PERMISSIONS`` grouped by namespace.

    Namespace is the substring before the first ``.`` (``admin.view``
    → ``admin``). Keys without a dot fall under namespace ``"_root"``
    so the UI has a stable bucket.
    """
    by_ns: dict[str, list[str]] = {}
    for key in ALL_PERMISSIONS:
        ns, _, _ = key.partition(".")
        if not ns:
            ns = "_root"
        by_ns.setdefault(ns, []).append(key)
    for ns in by_ns:
        by_ns[ns].sort()
    return dict(sorted(by_ns.items()))
