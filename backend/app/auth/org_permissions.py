"""Org-level role gating (L3.8).

Distinct from the platform-level `app.auth.permissions` module which
gates the `/admin` superadmin surface. This one keys off `User.role`
within the user's own organization (OWNER / ADMIN / MEMBER).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.deps import get_current_user
from app.models.user import Role, User


def require_org_admin(current_user: User = Depends(get_current_user)) -> User:
    """Pass when the requester is OWNER or ADMIN within their org.
    MEMBER → 403."""
    if current_user.role not in (Role.OWNER, Role.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or owner role required",
        )
    return current_user
