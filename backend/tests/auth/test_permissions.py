from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import app.auth.permissions as permissions_module
from app.auth.permissions import (
    ALL_PERMISSIONS,
    has_permission,
    require_permission,
)
from app.models.user import Role, User


def make_user(**overrides) -> User:
    base = {
        "org_id": 1,
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "hashed-password",
        "role": Role.OWNER,
        "is_superadmin": False,
        "is_active": True,
    }
    base.update(overrides)
    return User(**base)

def make_client(user: User) -> TestClient:
    app = FastAPI()

    async def current_user_override() -> AsyncIterator[User]:
        yield user

    app.dependency_overrides[permissions_module.get_current_user] = (
        current_user_override
    )

    @app.get("/protected")
    async def protected_route(
        _current_user: User = Depends(require_permission("plans.manage")),
    ):
        return {"ok": True}

    return TestClient(app)


def test_all_permissions_contains_known_platform_permissions() -> None:
    assert ALL_PERMISSIONS == frozenset({"admin.view", "plans.manage"})


def test_has_permission_grants_everything_to_superadmins() -> None:
    user = make_user(is_superadmin=True)

    assert has_permission(user, "admin.view") is True
    assert has_permission(user, "plans.manage") is True


def test_has_permission_denies_regular_users_by_default() -> None:
    user = make_user()

    assert has_permission(user, "plans.manage") is False


def test_has_permission_honors_role_permission_map(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions_module,
        "ROLE_PERMISSIONS",
        {"platform_operator": frozenset({"plans.manage"})},
    )
    monkeypatch.setattr(
        permissions_module,
        "_platform_roles",
        lambda _user: frozenset({"platform_operator"}),
    )

    assert has_permission(make_user(), "plans.manage") is True


def test_require_permission_allows_authorized_user() -> None:
    with make_client(make_user(is_superadmin=True)) as client:
        response = client.get("/protected")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_require_permission_returns_403_for_authenticated_user_without_permission() -> None:
    with make_client(make_user()) as client:
        response = client.get("/protected")

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_require_permission_dependency_name_is_stable() -> None:
    dependency = require_permission("plans.manage")

    assert dependency.__name__ == "require_permission_plans_manage"
