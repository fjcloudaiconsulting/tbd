"""Tests for require_org_owner — owner-only tenant gating (L3.1)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth.org_permissions import require_org_owner
from app.models.user import Role, User


def _user(role: Role) -> User:
    return User(
        id=1,
        username="u",
        email="u@x.io",
        password_hash="x",
        org_id=1,
        role=role,
        is_active=True,
    )


def test_require_org_owner_allows_owner():
    u = _user(Role.OWNER)
    assert require_org_owner(current_user=u) is u


def test_require_org_owner_rejects_admin():
    with pytest.raises(HTTPException) as exc:
        require_org_owner(current_user=_user(Role.ADMIN))
    assert exc.value.status_code == 403
    assert "Owner" in exc.value.detail


def test_require_org_owner_rejects_member():
    with pytest.raises(HTTPException) as exc:
        require_org_owner(current_user=_user(Role.MEMBER))
    assert exc.value.status_code == 403
