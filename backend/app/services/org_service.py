"""Tenant org-mutation services (Track D).

Initial scope is the OWNER-only rename. The wider set of org
self-management operations (transfer-ownership, etc.) lands in
later tracks; keeping the module narrow until then.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Organization


async def rename_org(
    db: AsyncSession,
    *,
    org_id: int,
    new_name: str,
) -> tuple[str, str]:
    """Rename ``org_id`` to ``new_name``.

    Returns ``(old_name, new_name)``. If the new name normalizes to
    the same value as the old name (case-insensitive, after the
    Pydantic-side whitespace collapse), returns ``(old_name, old_name)``
    and does NOT mutate the row — callers MUST detect this and skip
    both audit and commit.

    Caller responsibilities:
        - Provide a ``new_name`` already normalized by ``OrgRenameRequest``
          (trim + whitespace collapse + control-char rejection).
        - Commit the session after this call returns.
        - Translate ``IntegrityError`` raised on commit to a 409
          (the DB UNIQUE constraint on ``name_normalized`` is the
          backstop against the friendly preflight below losing a race).

    Raises:
        HTTPException(404): organization does not exist.
        HTTPException(409): preflight finds another org with the same
            case-insensitive name.
    """
    org = (
        await db.execute(
            select(Organization)
            .where(Organization.id == org_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    old_name = org.name

    # Same-name guard. Pydantic already collapsed whitespace, but
    # keep the strip()/lower() defensive here so the service can be
    # reused safely by future internal callers that bypass Pydantic.
    if old_name.lower().strip() == new_name.lower().strip():
        return (old_name, old_name)

    # Friendly preflight. The DB UNIQUE constraint is the source of
    # truth, but a clean 409 with a helpful message beats raising
    # IntegrityError up to the user. The race window between this
    # SELECT and the COMMIT is closed by the constraint.
    #
    # The actual UNIQUE on MySQL lives on a generated ``name_normalized``
    # column with collation ``utf8mb4_0900_as_cs`` — case-insensitive
    # but accent-sensitive. The base ``organizations.name`` column uses
    # the table's default collation (typically ``utf8mb4_0900_ai_ci``,
    # accent-insensitive), so SQL operators on the base column treat
    # "Cafe" and "Café" as equal. To stay aligned with the constraint,
    # SQL pre-filters loosely on ``LOWER(name)`` (over-accepts on
    # MySQL: matches accent variants too) and Python filters exactly
    # with ``str.lower()`` (preserves accents).
    #
    # Also avoids ``ilike(new_name)``: ``%``/``_`` would be parsed as
    # wildcards and could falsely match unrelated names.
    new_lower = new_name.lower()
    candidates = (
        await db.execute(
            select(Organization.id, Organization.name)
            .where(Organization.id != org_id)
            .where(func.lower(Organization.name) == new_lower)
        )
    ).all()
    for row in candidates:
        # Python's str.lower() is accent-preserving, mirroring the
        # accent-sensitive UNIQUE on MySQL.
        if row.name.lower() == new_lower:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An organization with that name already exists",
            )

    org.name = new_name
    return (old_name, new_name)
