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
from fastapi import FastAPI, Request
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

    async def override_current_user(request: Request) -> User:
        request.state.auth_method = "jwt"  # interactive session (TBD-626)
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
# Validate-endpoint cooldown (spec §6 T10): per-(org, credential) 5 s,
# counted in the limits DB (rate limits move to MySQL, INFRA-121).
# ----------------------------------------------------------------


async def _two_credentials(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    with _patch_adapter(_ok_validate()):
        a = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-cd3-aaaa"},
        )
        b = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "anthropic", "api_key": "sk-ant-test-cd-bbbb"},
        )
    return client, a.json()["id"], b.json()["id"]


async def test_a3_validate_cooldown_is_5s_per_credential(session_factory, monkeypatch):
    """A3. Wrong implementation: no cooldown, or one that never expires."""
    from app import rate_limit_db

    t = [5000.0]
    monkeypatch.setattr(rate_limit_db, "_clock", lambda: t[0])
    client, a_id, b_id = await _two_credentials(session_factory)
    url = "/api/v1/settings/ai-providers/{}/validate"
    with _patch_adapter(_ok_validate()):
        assert client.post(url.format(a_id)).status_code == 200
        second = client.post(url.format(a_id))
        assert client.post(url.format(b_id)).status_code == 200  # own bucket
        t[0] += 5.0
        assert client.post(url.format(a_id)).status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "validate_rate_limited"


# ---------------------------------------------------------------------------
# TBD-584: bring-your-own local models (ollama) and the native stub are gone.
# Create refuses them; a stored row that still names one stays inert.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "ollama", "base_url": "http://ollama.internal:11434"},
        {"provider": "ollama", "api_key": "sk-abcd", "base_url": "https://ollama.example.com"},
        {"provider": "native", "api_key": "sk-abcd"},
        {"provider": "openai", "api_key": "sk-abcd", "bearer_token": "tok-abcd"},
    ],
    ids=["ollama-no-key", "ollama-key", "native", "bearer-token"],
)
async def test_create_refuses_retired_providers_with_422(session_factory, body):
    from sqlalchemy import func, select
    from app.models.org_ai_credential import OrgAICredential

    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    validate = _ok_validate(["llama3"])
    with _patch_adapter(validate):
        resp = client.post("/api/v1/settings/ai-providers", json=body)
    assert resp.status_code == 422, resp.text
    validate.assert_not_called()
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OrgAICredential)) == 0


async def _insert_retired(factory, org_id: int, provider: str = "ollama") -> int:
    """A row as an earlier release stored it: raw SQL, because the ORM
    refuses a provider outside the registry on write."""
    from sqlalchemy import text

    async with factory() as db:
        await db.execute(
            text(
                "INSERT INTO org_ai_credentials "
                "(org_id, provider, base_url, encrypted_bearer_token, label, "
                "discovered_capabilities, discovered_models) "
                "VALUES (:org, :provider, 'http://10.0.0.5:11434', 'ciphertext', "
                "'lan', '[\"chat\"]', '[\"llama3\"]')"
            ),
            {"org": org_id, "provider": provider},
        )
        cred_id = (await db.execute(text("SELECT max(id) FROM org_ai_credentials"))).scalar()
        await db.commit()
    return cred_id


@pytest.mark.parametrize("provider", ["ollama", "native"])
async def test_retired_provider_row_is_listed_flagged_and_deletable(
    session_factory, provider
):
    from sqlalchemy import func, select
    from app.models.org_ai_routing import OrgAIDefaultRouting

    ids = await _seed(session_factory)
    retired = await _insert_retired(session_factory, ids["org_a"], provider)
    async with session_factory() as db:
        db.add(OrgAIDefaultRouting(org_id=ids["org_a"], credential_id=retired, model="llama3"))
        await db.commit()

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    with _patch_adapter(_ok_validate()):
        created = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-abcd"},
        ).json()
    assert created["provider"] == "openai"
    assert created["provider_supported"] is True

    listed = client.get("/api/v1/settings/ai-providers?sort_by=provider")
    assert listed.status_code == 200, listed.text
    by_id = {i["id"]: i for i in listed.json()["items"]}
    assert by_id[retired]["provider"] == provider
    assert by_id[retired]["provider_supported"] is False
    assert "encrypted_bearer_token" not in by_id[retired]

    one = client.get(f"/api/v1/settings/ai-providers/{retired}")
    assert one.status_code == 200
    assert one.json()["provider_supported"] is False

    routing = client.get("/api/v1/settings/ai-providers/routing")
    assert routing.status_code == 200
    assert routing.json()["default"]["credential_id"] == retired

    assert client.patch(
        f"/api/v1/settings/ai-providers/{retired}", json={"label": "old"}
    ).json()["label"] == "old"

    assert client.delete(f"/api/v1/settings/ai-providers/{retired}").status_code == 204
    async with session_factory() as db:
        # The routing row pointing at it cascades with it.
        assert await db.scalar(select(func.count()).select_from(OrgAIDefaultRouting)) == 0
    assert client.get(f"/api/v1/settings/ai-providers/{retired}").status_code == 404


async def test_retired_provider_row_never_reaches_an_adapter(session_factory):
    ids = await _seed(session_factory)
    retired = await _insert_retired(session_factory, ids["org_a"])

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    validate = _ok_validate(["llama3"])
    with _patch_adapter(validate):
        for resp in (
            client.post(f"/api/v1/settings/ai-providers/{retired}/validate"),
            client.post(
                f"/api/v1/settings/ai-providers/{retired}/rotate",
                json={"api_key": "sk-new-abcd"},
            ),
        ):
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["code"] == "ai_provider_not_supported"
    validate.assert_not_called()

    models = client.get(f"/api/v1/settings/ai-providers/{retired}/models")
    assert models.status_code == 200
    assert models.json()["models"] == []
    put = client.put(
        "/api/v1/settings/ai-providers/routing/default",
        json={"credential_id": retired, "model": "llama3"},
    )
    assert put.status_code == 400
    assert put.json()["detail"]["code"] == "model_not_listed"


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


async def test_f12_config_writes_do_not_need_the_ai_switch(session_factory):
    """FENCE TBD-627 F12 (C4): the org has NO consent row, and credential
    create, validate (refresh models), rotate and a routing write all work.
    Kills: the master switch spread onto configuration writes."""
    ids = await _seed(session_factory)

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    client = TestClient(_make_app(session_factory, resolver))
    with _patch_adapter(_ok_validate()):
        created = client.post(
            "/api/v1/settings/ai-providers",
            json={"provider": "openai", "api_key": "sk-test-f12-aaaa"},
        )
    assert created.status_code == 201, created.text
    cred_id = created.json()["id"]
    with _patch_adapter(_ok_validate(["gpt-5"])):
        assert client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate").status_code == 200
        assert client.post(
            f"/api/v1/settings/ai-providers/{cred_id}/rotate", json={"api_key": "sk-test-f12-bbbb"}
        ).status_code == 200
    put = client.put(
        "/api/v1/settings/ai-providers/routing/default",
        json={"credential_id": cred_id, "model": "gpt-5"},
    )
    assert put.status_code == 200, put.text


# ---------------------------------------------------------------------------
# TBD-609: a password stored in base_url (rows saved before DB-02) never
# leaves the server in a response or audit row, and still reaches the adapter.
# ---------------------------------------------------------------------------

_URL_SECRET = "s3cr3t-pw-tbd609"
_REAL_URL = f"https://user:{_URL_SECRET}@llm.example.com/v1"
_MASKED_URL = "https://***@llm.example.com/v1"
# Stored base_url -> fragments that must reach no response, audit row or log.
_LEGACY_URLS = [
    (_REAL_URL, [_URL_SECRET]),
    # urlsplit drops tab/CR/LF before parsing, so the parsed netloc is not a
    # substring of the raw value (security review of #1121).
    ("https://user:s3cr3t\t-pw@llm.example.com/v1", ["s3cr3t"]),
    ("https://user\n:s3cr3t-pw@llm.example.com/v1", ["s3cr3t"]),
    # The username can be the secret and the password a dummy.
    ("https://apikey:x@llm.example.com/v1", ["apikey", ":x@"]),
]


async def _userinfo_row(session_factory, base_url=_REAL_URL):
    from fastapi.exceptions import RequestValidationError

    from app.main import request_validation_handler
    from app.models.org_ai_credential import AiProvider, OrgAICredential
    from app.services.ai_credential_crypto import encrypt

    ids = await _seed(session_factory)
    async with session_factory() as db:
        row = OrgAICredential(
            org_id=ids["org_a"], provider=AiProvider.OPENAI_COMPATIBLE,
            encrypted_api_key=encrypt("sk-legacy-abcd"), last_four="abcd",
            base_url=base_url, base_url_is_api_root=True, label="legacy",
        )
        db.add(row)
        await db.commit()
        cred_id = row.id

    async def resolver(_factory):
        return await _get_user(session_factory, ids["owner_a"])

    app = _make_app(session_factory, resolver)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    return TestClient(app), cred_id


async def _stored_url(session_factory, cred_id):
    from app.models.org_ai_credential import OrgAICredential

    async with session_factory() as db:
        return (await db.get(OrgAICredential, cred_id)).base_url


@pytest.mark.parametrize(("stored", "secrets"), _LEGACY_URLS)
async def test_url_password_is_in_no_credential_response(session_factory, caplog, stored, secrets):
    from sqlalchemy import select

    from app.models.audit_event import AuditEvent

    caplog.set_level("DEBUG", logger="app")  # sqlalchemy/httpx stay at WARNING in prod (app.logs)
    client, cred_id = await _userinfo_row(session_factory, stored)
    base = "/api/v1/settings/ai-providers"
    with _patch_adapter(_ok_validate()):
        responses = {
            "create": client.post(base, json={
                "provider": "openai_compatible", "api_key": "sk-new-abcd",
                "base_url": stored,
            }),
            "list": client.get(base),
            "get": client.get(f"{base}/{cred_id}"),
            "update": client.patch(f"{base}/{cred_id}", json={"label": "renamed"}),
            "rotate": client.post(f"{base}/{cred_id}/rotate", json={"api_key": "sk-rot-wxyz"}),
            "validate": client.post(f"{base}/{cred_id}/validate"),
        }
    assert responses["create"].status_code == 422  # DB-02 refuses new userinfo
    for name, resp in responses.items():
        for secret in secrets:
            assert secret not in resp.text, (name, resp.text)
    for name in ("get", "update", "rotate", "validate"):
        assert responses[name].status_code == 200, (name, responses[name].text)
        assert responses[name].json()["base_url"] == _MASKED_URL, name
    assert responses["list"].json()["items"][0]["base_url"] == _MASKED_URL

    async with session_factory() as db:
        events = (await db.execute(select(AuditEvent))).scalars().all()
    assert events  # the writes above were audited
    for secret in secrets:
        for ev in events:
            assert secret not in repr(ev.detail), ev.event_type
        assert secret not in caplog.text
    assert await _stored_url(session_factory, cred_id) == stored


async def test_masked_base_url_sent_back_never_overwrites_the_stored_one(session_factory):
    """Round trip: the edit route is label-only (extra=forbid), so a client that
    echoes the masked URL gets a 422 and the stored password survives."""
    client, cred_id = await _userinfo_row(session_factory)
    resp = client.patch(
        f"/api/v1/settings/ai-providers/{cred_id}",
        json={"label": "renamed", "base_url": _MASKED_URL},
    )
    assert resp.status_code == 422
    assert _URL_SECRET not in resp.text
    assert await _stored_url(session_factory, cred_id) == _REAL_URL


async def test_adapter_still_gets_the_real_base_url(session_factory):
    client, cred_id = await _userinfo_row(session_factory)
    validate = _ok_validate()
    adapter = type("FakeAdapter", (), {"validate": validate})()
    with patch(
        "app.services.ai_credential_service.get_adapter", return_value=adapter
    ) as get_adapter:
        assert client.post(f"/api/v1/settings/ai-providers/{cred_id}/validate").status_code == 200
        assert client.post(
            f"/api/v1/settings/ai-providers/{cred_id}/rotate", json={"api_key": "sk-rot-wxyz"}
        ).status_code == 200
    assert get_adapter.call_count == 2
    for call in get_adapter.call_args_list:
        assert call.kwargs["base_url"] == _REAL_URL
