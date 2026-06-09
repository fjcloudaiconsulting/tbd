"""Router tests for /api/v1/settings/ai-providers (PR1).

Pins:
- GET response NEVER includes encrypted_* fields.
- Non-admin (MEMBER) gets 403.
- Cross-org isolation: org A admin reading org B's credential gets 404.
- Happy CRUD paths with the adapter mocked.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.routers.ai_providers import router as ai_providers_router
from app.security import hash_password
from app.services.ai_providers.base import ValidateResult


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(ai_providers_router)
    return app


async def _seed(factory):
    async with factory() as db:
        org_a = Organization(name="Acme", billing_cycle_day=1)
        org_b = Organization(name="Globex", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        owner_a = User(
            org_id=org_a.id,
            username="a_owner",
            email="a_owner@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        member_a = User(
            org_id=org_a.id,
            username="a_member",
            email="a_member@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        owner_b = User(
            org_id=org_b.id,
            username="b_owner",
            email="b_owner@globex.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add_all([owner_a, member_a, owner_b])
        await db.commit()
        return {
            "org_a": org_a.id,
            "org_b": org_b.id,
            "owner_a": owner_a.id,
            "member_a": member_a.id,
            "owner_b": owner_b.id,
        }


async def _get_user(factory, user_id: int) -> User:
    from sqlalchemy import select
    async with factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one()


def _ok_validate(models=None):
    return AsyncMock(return_value=ValidateResult(
        ok=True,
        discovered_models=list(models or ["gpt-4o-mini"]),
        discovered_capabilities=["chat", "embed"],
    ))


def _patch_adapter(validate_mock):
    adapter = type("FakeAdapter", (), {"validate": validate_mock})()
    return patch(
        "app.services.ai_credential_service.get_adapter",
        return_value=adapter,
    )


async def test_list_returns_sanitized_response_no_encrypted_fields(
    session_factory,
):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)

    # Seed one credential.
    with _patch_adapter(_ok_validate()):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={
                "provider": "openai",
                "api_key": "sk-test-very-secret-abcd",
                "label": "prod",
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert "encrypted_api_key" not in body
    assert "encrypted_bearer_token" not in body
    assert body["last_four"] == "abcd"

    list_resp = client.get("/api/v1/settings/ai-providers")
    assert list_resp.status_code == 200
    list_body = list_resp.json()
    assert list_body["total"] == 1
    items = list_body["items"]
    assert len(items) == 1
    assert "encrypted_api_key" not in items[0]
    assert items[0]["last_four"] == "abcd"


async def test_member_role_gets_403(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["member_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    resp = client.get("/api/v1/settings/ai-providers")
    assert resp.status_code == 403


async def test_cross_org_get_returns_404(session_factory):
    ids = await _seed(session_factory)

    # Owner of org A seeds a credential.
    async def resolver_a(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver_a)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-cross-org-aaaa"},
        )
    assert resp.status_code == 201
    cred_id = resp.json()["id"]

    # Owner of org B tries to GET that credential by id.
    async def resolver_b(_factory):
        return await _get_user(session_factory, ids["owner_b"])

    app_b = _make_app(session_factory, resolver_b)
    client_b = TestClient(app_b)
    resp_b = client_b.get(f"/api/v1/settings/ai-providers/{cred_id}")
    assert resp_b.status_code == 404


async def test_create_then_get_returns_metadata(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate(["gpt-4o"])):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-abcd1234"},
        )
    assert resp.status_code == 201
    cred_id = resp.json()["id"]
    got = client.get(f"/api/v1/settings/ai-providers/{cred_id}")
    assert got.status_code == 200
    assert got.json()["discovered_models"] == ["gpt-4o"]


async def test_rotate_updates_last_four(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-orig-aaaa"},
        )
    cred_id = resp.json()["id"]
    with _patch_adapter(_ok_validate()):
        rot = client.post(
            f"/api/v1/settings/ai-providers/{cred_id}/rotate",
            json={"api_key": "sk-test-rotated-9999"},
        )
    assert rot.status_code == 200
    assert rot.json()["last_four"] == "9999"


async def test_validate_endpoint_returns_response(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-val-aaaa"},
        )
    cred_id = resp.json()["id"]
    with _patch_adapter(_ok_validate(["gpt-5"])):
        val = client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate")
    assert val.status_code == 200
    assert val.json()["discovered_models"] == ["gpt-5"]


async def test_patch_label_only(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-lbl-aaaa", "label": "old"},
        )
    cred_id = resp.json()["id"]
    pat = client.patch(
        f"/api/v1/settings/ai-providers/{cred_id}",
        json={"label": "new"},
    )
    assert pat.status_code == 200
    assert pat.json()["label"] == "new"


async def test_delete_removes_credential(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-del-aaaa"},
        )
    cred_id = resp.json()["id"]
    delr = client.delete(f"/api/v1/settings/ai-providers/{cred_id}")
    assert delr.status_code == 204
    get = client.get(f"/api/v1/settings/ai-providers/{cred_id}")
    assert get.status_code == 404


async def test_bad_validation_returns_400_and_no_row_persisted(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    bad_mock = AsyncMock(return_value=ValidateResult(ok=False, error="Unauthorized"))
    with _patch_adapter(bad_mock):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-bad-aaaa"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "credential_validation_failed"
    listr = client.get("/api/v1/settings/ai-providers")
    assert listr.json()["items"] == []
    assert listr.json()["total"] == 0


# ----------------------------------------------------------------
# Validate-endpoint cooldown (architect round-3 blocker, spec §6 T10).
# Per-(org, credential) 5 s cooldown via redis SET NX EX. Tests stub
# the redis helper so they don't require a live Redis.
# ----------------------------------------------------------------


async def test_validate_second_call_within_5s_returns_429(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        post = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-cd-aaaa"},
        )
    cred_id = post.json()["id"]

    # First call: cooldown free. Second call within the TTL window:
    # cooldown held. Mocks return True then False to simulate the
    # SET NX EX semantics without needing a live Redis or sleep.
    acquire_mock = AsyncMock(side_effect=[True, False])
    with patch(
        "app.routers.ai_providers.redis_client.ai_validate_cooldown_acquire",
        new=acquire_mock,
    ):
        with _patch_adapter(_ok_validate()):
            first = client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate")
        assert first.status_code == 200
        second = client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate")
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "validate_rate_limited"


async def test_validate_after_5s_succeeds(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        post = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-cd2-aaaa"},
        )
    cred_id = post.json()["id"]

    # First call: claim. Time passes, TTL expires. Second call: claim
    # again. Simulate by returning True both times (the cooldown helper
    # would return True on the second call once the prior key has
    # expired).
    acquire_mock = AsyncMock(side_effect=[True, True])
    with patch(
        "app.routers.ai_providers.redis_client.ai_validate_cooldown_acquire",
        new=acquire_mock,
    ):
        with _patch_adapter(_ok_validate()):
            first = client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate")
            second = client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate")
    assert first.status_code == 200
    assert second.status_code == 200


async def test_validate_different_credentials_independent(session_factory):
    """Each (org, credential_id) tuple has its own cooldown bucket.

    Two credentials in the same org can be validated back-to-back even
    inside the 5 s window — independent buckets.
    """
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        a = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-cd3-aaaa"},
        )
        b = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "anthropic", "api_key": "sk-ant-test-cd-bbbb"},
        )
    a_id = a.json()["id"]
    b_id = b.json()["id"]

    # The helper is keyed on (org_id, credential_id) so each call gets
    # its own slot — both acquire True.
    acquire_mock = AsyncMock(side_effect=[True, True])
    with patch(
        "app.routers.ai_providers.redis_client.ai_validate_cooldown_acquire",
        new=acquire_mock,
    ):
        with _patch_adapter(_ok_validate()):
            r_a = client.post(f"/api/v1/settings/ai-providers/{a_id}/validate")
            r_b = client.post(f"/api/v1/settings/ai-providers/{b_id}/validate")
    assert r_a.status_code == 200
    assert r_b.status_code == 200
    # Confirm the helper was keyed per-credential.
    assert acquire_mock.call_args_list[0].kwargs["credential_id"] == a_id
    assert acquire_mock.call_args_list[1].kwargs["credential_id"] == b_id


async def test_ollama_no_key_creates_credential_with_null_encrypted_api_key(
    session_factory,
):
    """POST Ollama credential without api_key must return 201 and store
    encrypted_api_key as NULL (spec line 37: LAN-only homelab mode)."""
    from sqlalchemy import select
    from app.models.org_ai_credential import OrgAICredential

    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)

    with _patch_adapter(_ok_validate(["llama3"])):
        resp = client.post(
            "/api/v1/settings/ai-providers",
            json={
                "provider": "ollama",
                "base_url": "http://ollama.internal:11434",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["provider"] == "ollama"
    assert "encrypted_api_key" not in body

    # Confirm the DB row has encrypted_api_key IS NULL.
    cred_id = body["id"]
    async with session_factory() as db:
        result = await db.execute(
            select(OrgAICredential).where(OrgAICredential.id == cred_id)
        )
        row = result.scalar_one()
    assert row.encrypted_api_key is None


async def test_validate_falls_open_when_redis_unavailable(
    session_factory, caplog
):
    """When the Redis cooldown helper raises a transport error, the
    validate endpoint should fail-open (200, not 500). The cooldown is
    a T10 abuse mitigation, not a security boundary — the org-admin
    gate is the boundary. A Redis outage must not block legitimate
    credential management. A structured warning is logged so ops can
    notice the degraded state.
    """
    import logging
    from redis.exceptions import ConnectionError as RedisConnectionError

    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    with _patch_adapter(_ok_validate()):
        post = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-failopen-aaaa"},
        )
    cred_id = post.json()["id"]

    # Patch the cooldown helper itself (the public one the router
    # calls) to behave the way the real helper does on Redis outage:
    # fail-open and return True. The helper internally catches
    # RedisError and logs ``ai.validate.cooldown.fail_open``; the unit
    # test for the helper's catch behavior lives in
    # ``tests/test_redis_client.py``. Here we pin the router contract:
    # the validate path returns 200 on a fail-open cooldown decision.
    acquire_mock = AsyncMock(return_value=True)
    with caplog.at_level(logging.WARNING):
        with patch(
            "app.routers.ai_providers.redis_client.ai_validate_cooldown_acquire",
            new=acquire_mock,
        ):
            with _patch_adapter(_ok_validate()):
                resp = client.post(
                    f"/api/v1/settings/ai-providers/{cred_id}/validate"
                )

    assert resp.status_code == 200, resp.text
    # Direct unit check of the helper's fail-open behavior — when the
    # underlying SET raises RedisError, the public helper returns True
    # (acquired) so callers proceed. We exercise it here so a single
    # test pins both the helper contract and the router-path 200.
    from app import redis_client as _redis_client

    with patch.object(
        _redis_client,
        "_ai_validate_cooldown_set",
        new=AsyncMock(side_effect=RedisConnectionError("boom")),
    ), patch.object(
        _redis_client,
        "get_client",
        return_value=object(),
    ):
        ok = await _redis_client.ai_validate_cooldown_acquire(
            org_id=1, credential_id=1
        )
    assert ok is True


async def test_list_unknown_sort_is_400(session_factory):
    """Closed sort whitelist: an unknown sort_by is a 400, never a
    silent fallback (Tables PR2)."""
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    resp = client.get("/api/v1/settings/ai-providers?sort_by=encrypted_api_key")
    assert resp.status_code == 400
