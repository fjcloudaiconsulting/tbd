from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.deps import get_current_user


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
