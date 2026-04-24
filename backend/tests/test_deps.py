from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

import app.deps as deps_module
from app.database import get_db
from app.deps import get_current_user
from app.models.user import Role, User


async def override_get_db() -> AsyncIterator[None]:
    yield None


def make_client() -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_db] = override_get_db

    @app.get("/protected")
    async def protected_route(_current_user=Depends(get_current_user)):
        return {"ok": True}

    return TestClient(app)


def test_get_current_user_returns_403_when_header_is_missing() -> None:
    with make_client() as client:
        response = client.get("/protected")

    assert response.status_code == 403


def test_get_current_user_returns_401_for_invalid_bearer_token() -> None:
    with make_client() as client:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeAsyncSession:
    def __init__(self, value):
        self._value = value

    async def execute(self, _statement):
        return FakeResult(self._value)


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


def make_credentials() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token")


@pytest.mark.asyncio
async def test_get_current_user_returns_user_for_valid_access_token_without_iat(
    monkeypatch,
) -> None:
    user = make_user()
    db = FakeAsyncSession(user)

    monkeypatch.setattr(
        deps_module,
        "decode_token",
        lambda _token: {"sub": "1", "type": "access"},
    )

    resolved = await get_current_user(make_credentials(), db)

    assert resolved is user


@pytest.mark.asyncio
async def test_get_current_user_rejects_non_access_tokens(monkeypatch) -> None:
    monkeypatch.setattr(
        deps_module,
        "decode_token",
        lambda _token: {"sub": "1", "type": "refresh"},
    )

    with pytest.raises(HTTPException, match="Invalid or expired token") as exc:
        await get_current_user(make_credentials(), FakeAsyncSession(make_user()))

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_rejects_inactive_users(monkeypatch) -> None:
    monkeypatch.setattr(
        deps_module,
        "decode_token",
        lambda _token: {"sub": "1", "type": "access"},
    )

    with pytest.raises(HTTPException, match="User not found or inactive") as exc:
        await get_current_user(
            make_credentials(),
            FakeAsyncSession(make_user(is_active=False)),
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_rejects_tokens_issued_before_cutoff(monkeypatch) -> None:
    issued_at = int(datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    cutoff = datetime(2026, 4, 24, 12, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        deps_module,
        "decode_token",
        lambda _token: {"sub": "1", "type": "access", "iat": issued_at},
    )
    monkeypatch.setattr(deps_module, "token_cutoff", lambda _user: cutoff)

    with pytest.raises(HTTPException, match="Session has been invalidated") as exc:
        await get_current_user(make_credentials(), FakeAsyncSession(make_user()))

    assert exc.value.status_code == 401
