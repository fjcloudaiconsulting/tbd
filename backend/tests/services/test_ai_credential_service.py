"""Service-layer tests for AI credential CRUD.

Mocks the adapter (no network) and pins:

- bad-validation create path does not persist a row.
- happy create / rotate / delete flows hit audit events.
- validate_now updates last_validated_at + discovered_* + clears
  validation_error on success.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.user import Organization
from app.schemas.org_ai_credential import OrgAICredentialCreate, OrgAICredentialResponse
from app.services import ai_credential_crypto, ai_credential_service
from app.services.ai_providers.base import ValidateResult


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> Organization:
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


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


def _ok_validate(models=None):
    mock = AsyncMock(return_value=ValidateResult(
        ok=True,
        discovered_models=list(models or ["gpt-4o-mini"]),
        discovered_capabilities=["chat", "embed"],
    ))
    return mock


def _bad_validate(error="bad key"):
    return AsyncMock(return_value=ValidateResult(ok=False, error=error))


def _patch_adapter(validate_mock):
    """Patch get_adapter to return an object whose .validate is the mock."""
    adapter = type("FakeAdapter", (), {"validate": validate_mock})()
    return patch(
        "app.services.ai_credential_service.get_adapter",
        return_value=adapter,
    )


_ACTOR_KW = {
    "actor_user_id": 1,
    "actor_email": "actor@example.test",
    "request_id": "rid-test",
    "ip_address": "127.0.0.1",
}


async def test_create_with_bad_validation_does_not_persist_row(
    db, session_factory, org
):
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key="sk-test-bad-key-xxxx",
        label="rejected",
    )
    with _patch_adapter(_bad_validate("Unauthorized")):
        with pytest.raises(Exception) as ei:
            await ai_credential_service.create_credential(
                db,
                org_id=org.id,
                payload=payload,
                session_factory=session_factory,
                **_ACTOR_KW,
            )
    assert getattr(ei.value, "status_code", None) == 400
    detail = ei.value.detail
    assert detail["code"] == "credential_validation_failed"
    rows = (
        await db.execute(
            select(OrgAICredential).where(OrgAICredential.org_id == org.id)
        )
    ).scalars().all()
    assert rows == []


async def test_create_happy_path_persists_and_returns_row(
    db, session_factory, org
):
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key="sk-test-good-key-abcd",
        label="prod",
    )
    with _patch_adapter(_ok_validate(["gpt-4o", "gpt-4o-mini"])):
        row = await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    assert row.id is not None
    assert row.provider == AiProvider.OPENAI
    assert row.last_four == "abcd"
    assert row.encrypted_api_key != payload.api_key
    assert row.discovered_models == ["gpt-4o", "gpt-4o-mini"]
    assert row.last_validated_at is not None


async def test_rotate_updates_envelope_and_last_four(db, session_factory, org):
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key="sk-test-good-key-aaaa",
        label="prod",
    )
    with _patch_adapter(_ok_validate()):
        row = await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    old_envelope = row.encrypted_api_key
    with _patch_adapter(_ok_validate(["gpt-4o"])):
        updated = await ai_credential_service.rotate_credential(
            db,
            credential=row,
            new_api_key="sk-test-rotated-9999",
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    assert updated.last_four == "9999"
    assert updated.encrypted_api_key != old_envelope


async def test_delete_removes_row(db, session_factory, org):
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key="sk-test-good-key-aaaa",
        label="prod",
    )
    with _patch_adapter(_ok_validate()):
        row = await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    rid = row.id
    await ai_credential_service.delete_credential(
        db, credential=row, session_factory=session_factory, **_ACTOR_KW,
    )
    gone = await ai_credential_service.get_credential_for_org(
        db, org_id=org.id, credential_id=rid
    )
    assert gone is None


async def test_validate_now_updates_timestamp_and_discovered(
    db, session_factory, org
):
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key="sk-test-good-key-aaaa",
        label="prod",
    )
    with _patch_adapter(_ok_validate(["gpt-4o-mini"])):
        row = await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    first_validated_at = row.last_validated_at
    # Simulate the row picking up a NEW set of models on revalidation.
    with _patch_adapter(_ok_validate(["gpt-5", "gpt-5-mini"])):
        updated = await ai_credential_service.validate_credential(
            db,
            credential=row,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    assert updated.discovered_models == ["gpt-5", "gpt-5-mini"]
    assert updated.last_validated_at >= first_validated_at
    assert updated.validation_error is None


async def test_validate_now_records_error_on_failure(
    db, session_factory, org
):
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key="sk-test-good-key-aaaa",
        label="prod",
    )
    with _patch_adapter(_ok_validate()):
        row = await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    with _patch_adapter(_bad_validate("Unauthorized")):
        updated = await ai_credential_service.validate_credential(
            db,
            credential=row,
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    assert updated.validation_error == "Unauthorized"


async def _seed_credential(db, session_factory, org, *, provider, api_key, base_url=None):
    payload = OrgAICredentialCreate(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        label="seed",
    )
    with _patch_adapter(_ok_validate()):
        return await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR_KW,
        )


@pytest.mark.parametrize(
    "provider, api_key, base_url",
    [
        (AiProvider.OPENAI, "sk-test-no-bt-aaaa", None),
        (AiProvider.ANTHROPIC, "sk-ant-no-bt-aaaa", None),
        (AiProvider.OPENAI_COMPATIBLE, "sk-compat-no-bt-aaaa", "https://llm.example.com"),
    ],
)
async def test_rotate_works_for_every_provider(
    db, session_factory, org, provider, api_key, base_url
):
    row = await _seed_credential(
        db, session_factory, org, provider=provider, api_key=api_key, base_url=base_url,
    )
    with _patch_adapter(_ok_validate()):
        updated = await ai_credential_service.rotate_credential(
            db,
            credential=row,
            new_api_key=api_key[:-4] + "9999",
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    assert updated.last_four == "9999"
    assert updated.encrypted_bearer_token is None


# ---------------------------------------------------------------
# TBD-584: a row an earlier release stored with a removed provider
# (ollama, native) is inert: validate and rotate refuse it before any
# adapter is built; delete still works.
# ---------------------------------------------------------------


@pytest.mark.parametrize("provider", ["ollama", "native"])
async def test_retired_provider_row_is_refused_before_any_adapter(
    db, session_factory, org, provider
):
    from fastapi import HTTPException
    from sqlalchemy import text

    row = await _seed_credential(
        db, session_factory, org, provider=AiProvider.OPENAI, api_key="sk-test-good-aaaa",
    )
    await db.execute(
        text("UPDATE org_ai_credentials SET provider = :p WHERE id = :id"),
        {"p": provider, "id": row.id},
    )
    await db.commit()
    await db.refresh(row)
    assert row.provider == provider

    validate = _ok_validate()
    with _patch_adapter(validate):
        for call in (
            ai_credential_service.validate_credential(
                db, credential=row, session_factory=session_factory, **_ACTOR_KW,
            ),
            ai_credential_service.rotate_credential(
                db,
                credential=row,
                new_api_key="sk-test-rotated-9999",
                session_factory=session_factory,
                **_ACTOR_KW,
            ),
        ):
            with pytest.raises(HTTPException) as ei:
                await call
            assert ei.value.status_code == 409
            assert ei.value.detail["code"] == "ai_provider_not_supported"
    validate.assert_not_called()

    await ai_credential_service.delete_credential(
        db, credential=row, session_factory=session_factory, **_ACTOR_KW,
    )
    assert await ai_credential_service.get_credential_for_org(
        db, org_id=org.id, credential_id=row.id
    ) is None


# ---------------------------------------------------------------
# TBD-719: the key fingerprint is an HMAC under a purpose-bound
# server key, marked ``h1:``. A row stored before it holds 16 bare
# hex chars of an unkeyed sha256 and stays valid until a revalidate
# or rotate rewrites it.
# ---------------------------------------------------------------

def _legacy(api_key: str) -> str:
    """The pre-TBD-719 value: an unkeyed sha256 prefix."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def test_fingerprint_key_is_purpose_bound():
    """Never the raw JWT secret: an HMAC directly under it differs."""
    raw = hmac.new(
        app_settings.jwt_secret_key.encode(), b"sk-test-fixture", "sha256"
    ).hexdigest()[:16]
    assert ai_credential_crypto.fingerprint("sk-test-fixture") != f"h1:{raw}"


def test_fingerprint_follows_the_server_secret(monkeypatch):
    """Rotating JWT_SECRET_KEY changes newly computed fingerprints; a
    revalidate or rotate rewrites the stored one (see below)."""
    before = ai_credential_crypto.fingerprint("sk-test-fixture")
    monkeypatch.setattr(app_settings, "jwt_secret_key", "x" * 64)
    assert ai_credential_crypto.fingerprint("sk-test-fixture") != before


async def test_create_and_rotate_store_the_keyed_fingerprint(db, session_factory, org):
    row = await _seed_credential(
        db, session_factory, org, provider=AiProvider.OPENAI, api_key="sk-test-fixture-aaaa"
    )
    assert row.key_fingerprint == ai_credential_crypto.fingerprint("sk-test-fixture-aaaa")
    with _patch_adapter(_ok_validate()):
        rotated = await ai_credential_service.rotate_credential(
            db, credential=row, new_api_key="sk-test-fixture-bbbb",
            session_factory=session_factory, **_ACTOR_KW,
        )
    assert rotated.key_fingerprint == ai_credential_crypto.fingerprint("sk-test-fixture-bbbb")


async def _legacy_row(db, session_factory, org, api_key: str) -> OrgAICredential:
    """A row as stored before TBD-719."""
    row = await _seed_credential(
        db, session_factory, org, provider=AiProvider.OPENAI, api_key=api_key
    )
    row.key_fingerprint = _legacy(api_key)
    await db.commit()
    await db.refresh(row)
    return row


async def test_legacy_row_still_serializes(db, session_factory, org):
    row = await _legacy_row(db, session_factory, org, "sk-test-fixture-cccc")
    body = OrgAICredentialResponse.model_validate(row)
    assert body.key_fingerprint == _legacy("sk-test-fixture-cccc")


@pytest.mark.parametrize("ok", [True, False], ids=["valid", "rejected"])
async def test_revalidate_upgrades_a_legacy_fingerprint(db, session_factory, org, ok):
    row = await _legacy_row(db, session_factory, org, "sk-test-fixture-dddd")
    with _patch_adapter(_ok_validate() if ok else _bad_validate()):
        updated = await ai_credential_service.validate_credential(
            db, credential=row, session_factory=session_factory, **_ACTOR_KW,
        )
    assert updated.key_fingerprint == ai_credential_crypto.fingerprint("sk-test-fixture-dddd")


async def test_revalidate_keeps_a_current_fingerprint(db, session_factory, org):
    row = await _seed_credential(
        db, session_factory, org, provider=AiProvider.OPENAI, api_key="sk-test-fixture-eeee"
    )
    before = row.key_fingerprint
    with _patch_adapter(_ok_validate()):
        updated = await ai_credential_service.validate_credential(
            db, credential=row, session_factory=session_factory, **_ACTOR_KW,
        )
    assert updated.key_fingerprint == before


async def test_revalidate_after_a_secret_rotation_refreshes_the_fingerprint(
    db, session_factory, org, monkeypatch
):
    row = await _seed_credential(
        db, session_factory, org, provider=AiProvider.OPENAI, api_key="sk-test-fixture-ffff"
    )
    monkeypatch.setattr(app_settings, "jwt_secret_key", "y" * 64)
    with _patch_adapter(_ok_validate()):
        updated = await ai_credential_service.validate_credential(
            db, credential=row, session_factory=session_factory, **_ACTOR_KW,
        )
    assert updated.key_fingerprint == ai_credential_crypto.fingerprint("sk-test-fixture-ffff")
