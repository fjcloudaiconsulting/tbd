"""Routing service: cross-org refusal at both layers + closed feature set.

Pins the architect-locked spec §4 behavior:

- service layer pre-checks ``credential.org_id == org_id`` and raises
  ``CrossOrgRoutingDenied`` with a clear message.
- the closed ROUTABLE_FEATURE_NAMES set rejects writes for arbitrary
  feature keys (UnknownFeatureName).
- ``get_routing_for_feature`` resolves per spec: feature override
  beats default; no row anywhere returns None.

DB-layer composite FK behavior is tested separately in
``tests/migrations/test_org_ai_routing_fk.py`` — SQLite (used here)
honors the composite FK with ``PRAGMA foreign_keys=ON`` so the safety
net fires there too.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import (
    OrgAIDefaultRouting,
    OrgAIFeatureRouting,
)
from app.models.user import Organization
from app.schemas.org_ai_credential import OrgAICredentialCreate
from app.services import ai_credential_service, ai_routing_service
from app.services.ai_providers.base import ValidateResult
from app.services.ai_routing_service import (
    CrossOrgRoutingDenied,
    UnknownFeatureName,
)


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


_ACTOR = {
    "actor_user_id": 1,
    "actor_email": "actor@example.test",
    "request_id": "rid",
    "ip_address": "127.0.0.1",
}


def _ok_validate():
    return AsyncMock(
        return_value=ValidateResult(
            ok=True,
            discovered_models=["gpt-4o-mini"],
            discovered_capabilities=["chat"],
        )
    )


async def _make_cred(db, session_factory, org_id: int) -> OrgAICredential:
    payload = OrgAICredentialCreate(
        provider=AiProvider.OPENAI,
        api_key=f"sk-{org_id}-test-1234",
        label=f"cred-{org_id}",
    )
    adapter = type("Fake", (), {"validate": _ok_validate()})()
    with patch(
        "app.services.ai_credential_service.get_adapter",
        return_value=adapter,
    ):
        return await ai_credential_service.create_credential(
            db,
            org_id=org_id,
            payload=payload,
            session_factory=session_factory,
            **_ACTOR,
        )


@pytest_asyncio.fixture
async def two_orgs_with_creds(db, session_factory):
    org_a = Organization(name="Acme", billing_cycle_day=1)
    org_b = Organization(name="Beta", billing_cycle_day=1)
    db.add_all([org_a, org_b])
    await db.commit()
    cred_a = await _make_cred(db, session_factory, org_a.id)
    cred_b = await _make_cred(db, session_factory, org_b.id)
    return org_a, org_b, cred_a, cred_b


async def test_set_default_routing_happy_path(db, session_factory, two_orgs_with_creds):
    org_a, _org_b, cred_a, _cred_b = two_orgs_with_creds
    row = await ai_routing_service.set_default_routing(
        db,
        org_id=org_a.id,
        credential_id=cred_a.id,
        model="gpt-4o-mini",
        session_factory=session_factory,
        **_ACTOR,
    )
    assert row.org_id == org_a.id
    assert row.credential_id == cred_a.id
    assert row.model == "gpt-4o-mini"


async def test_set_default_routing_refuses_cross_org(
    db, session_factory, two_orgs_with_creds
):
    org_a, _org_b, _cred_a, cred_b = two_orgs_with_creds
    with pytest.raises(CrossOrgRoutingDenied):
        await ai_routing_service.set_default_routing(
            db,
            org_id=org_a.id,
            credential_id=cred_b.id,
            model="gpt-4o-mini",
            session_factory=session_factory,
            **_ACTOR,
        )


async def test_db_composite_fk_refuses_cross_org_default(
    db, session_factory, two_orgs_with_creds
):
    """Belt-and-suspenders: even if the service check is bypassed, the
    composite FK fires. We bypass by writing the row directly via the
    ORM in a way that mimics a service-layer bug."""
    org_a, _org_b, _cred_a, cred_b = two_orgs_with_creds
    db.add(
        OrgAIDefaultRouting(
            org_id=org_a.id,
            credential_id=cred_b.id,  # belongs to org_b!
            model="gpt-4o-mini",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_default_routing_one_row_per_org_via_pk(
    db, session_factory, two_orgs_with_creds
):
    org_a, _org_b, cred_a, _cred_b = two_orgs_with_creds
    # First write creates the row.
    await ai_routing_service.set_default_routing(
        db,
        org_id=org_a.id,
        credential_id=cred_a.id,
        model="gpt-4o-mini",
        session_factory=session_factory,
        **_ACTOR,
    )
    # Second write updates it in place — there's still only one row.
    await ai_routing_service.set_default_routing(
        db,
        org_id=org_a.id,
        credential_id=cred_a.id,
        model="gpt-4o",
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await db.execute(
        select(OrgAIDefaultRouting).where(
            OrgAIDefaultRouting.org_id == org_a.id
        )
    )
    rows = list(res.scalars().all())
    assert len(rows) == 1
    assert rows[0].model == "gpt-4o"


async def test_feature_routing_refuses_unknown_feature(
    db, session_factory, two_orgs_with_creds
):
    org_a, _org_b, cred_a, _cred_b = two_orgs_with_creds
    with pytest.raises(UnknownFeatureName):
        await ai_routing_service.set_feature_routing(
            db,
            org_id=org_a.id,
            feature_name="not_a_real_feature",
            credential_id=cred_a.id,
            model="gpt-4o-mini",
            session_factory=session_factory,
            **_ACTOR,
        )


async def test_feature_routing_pk_uniqueness(
    db, session_factory, two_orgs_with_creds
):
    org_a, _org_b, cred_a, _cred_b = two_orgs_with_creds
    # First write inserts.
    await ai_routing_service.set_feature_routing(
        db,
        org_id=org_a.id,
        feature_name="categorize_transactions",
        credential_id=cred_a.id,
        model="gpt-4o-mini",
        session_factory=session_factory,
        **_ACTOR,
    )
    # Same (org, feature) re-set updates rather than inserting.
    await ai_routing_service.set_feature_routing(
        db,
        org_id=org_a.id,
        feature_name="categorize_transactions",
        credential_id=cred_a.id,
        model="gpt-4o",
        session_factory=session_factory,
        **_ACTOR,
    )
    rows = await ai_routing_service.get_feature_routings(db, org_id=org_a.id)
    assert len(rows) == 1
    assert rows[0].model == "gpt-4o"


async def test_get_routing_for_feature_resolution_order(
    db, session_factory, two_orgs_with_creds
):
    org_a, _org_b, cred_a, _cred_b = two_orgs_with_creds
    # No rows at all -> None.
    assert (
        await ai_routing_service.get_routing_for_feature(
            db, org_id=org_a.id, feature_name="categorize_transactions"
        )
        is None
    )
    # Just a default -> resolves to default.
    await ai_routing_service.set_default_routing(
        db,
        org_id=org_a.id,
        credential_id=cred_a.id,
        model="gpt-4o-mini",
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await ai_routing_service.get_routing_for_feature(
        db, org_id=org_a.id, feature_name="categorize_transactions"
    )
    assert res == (cred_a.id, "gpt-4o-mini")
    # A feature override beats the default.
    await ai_routing_service.set_feature_routing(
        db,
        org_id=org_a.id,
        feature_name="categorize_transactions",
        credential_id=cred_a.id,
        model="gpt-4o",
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await ai_routing_service.get_routing_for_feature(
        db, org_id=org_a.id, feature_name="categorize_transactions"
    )
    assert res == (cred_a.id, "gpt-4o")


async def test_delete_feature_routing_removes_row(
    db, session_factory, two_orgs_with_creds
):
    org_a, _org_b, cred_a, _cred_b = two_orgs_with_creds
    await ai_routing_service.set_feature_routing(
        db,
        org_id=org_a.id,
        feature_name="categorize_transactions",
        credential_id=cred_a.id,
        model="gpt-4o-mini",
        session_factory=session_factory,
        **_ACTOR,
    )
    assert (
        await ai_routing_service.delete_feature_routing(
            db,
            org_id=org_a.id,
            feature_name="categorize_transactions",
            session_factory=session_factory,
            **_ACTOR,
        )
        is True
    )
    assert (
        await ai_routing_service.delete_feature_routing(
            db,
            org_id=org_a.id,
            feature_name="categorize_transactions",
            session_factory=session_factory,
            **_ACTOR,
        )
        is False
    )
