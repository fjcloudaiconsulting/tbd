"""Platform-level authorization primitives.

The in-code role → permission map used to gate platform-only
(superadmin-scoped) endpoints. Design decisions — the permission-naming
convention, the is_superadmin short-circuit, the guard-API contract,
and the migration path to DB-backed RBAC — live in
docs/decisions/2026-04-24-platform-permissions.md. Do not restate that
rationale here; update the decision doc if it changes.
"""

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, HTTPException, status

from app.deps import get_current_user
from app.models.user import User


# Every permission the platform knows about. Adding a new one is a
# one-line edit here plus the corresponding gate at its call site.
Permission = Literal[
    "plans.manage",
]


# Canonical set — useful when iterating or seeding L4.8's eventual DB
# migration.
ALL_PERMISSIONS: frozenset[Permission] = frozenset({
    "plans.manage",
})


# Non-superadmin platform roles land here as they're introduced (L4.8
# role admin UI). Superadmin is intentionally absent: the is_superadmin
# short-circuit in has_permission() grants everything, so a new
# Permission is automatically available to superadmins.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {}


def _platform_roles(user: User) -> frozenset[str]:
    """Which platform roles this user holds.

    Source of truth today is the is_superadmin bool. When L4.8 adds a
    platform-role field or assignment table, this resolver is the only
    place that needs to change.
    """
    roles: set[str] = set()
    if user.is_superadmin:
        roles.add("superadmin")
    return frozenset(roles)


def has_permission(user: User, permission: Permission) -> bool:
    """True if the user is authorized for the given permission.

    Evaluation order is deliberate:
      1. Superadmin short-circuit — is_superadmin grants every
         permission, including ones added in the future.
      2. Role lookup via ROLE_PERMISSIONS. Unknown roles contribute no
         permissions; unknown permission strings resolve to False
         (deny-by-default) even when passed as str at a dynamic call
         site.
    """
    if user.is_superadmin:
        return True
    for role in _platform_roles(user):
        if permission in ROLE_PERMISSIONS.get(role, frozenset()):
            return True
    return False


def require_permission(
    permission: Permission,
) -> Callable[..., Awaitable[User]]:
    """FastAPI dependency factory — gates a route behind a permission.

    Auth and authz failures stay distinct:
      - get_current_user raises 401 on missing / invalid / expired token.
      - this dependency raises 403 on valid-token-without-permission.

    Returns the authenticated User on success, so a handler that needs
    the user can inject this dependency in its signature directly and
    skip declaring get_current_user a second time.
    """

    async def dependency(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if not has_permission(current_user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden",
            )
        return current_user

    dependency.__name__ = (
        f"require_permission_{permission.replace('.', '_')}"
    )
    return dependency
