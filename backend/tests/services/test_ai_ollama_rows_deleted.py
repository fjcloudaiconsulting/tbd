"""TBD-584: after migration 096 an org whose AI ran on Ollama is in the same
state as an org that never configured an AI provider.

The org has an Ollama credential (keyless, with a bearer token), a default
route and a feature route through it. The migration's delete runs on the
app's own schema with foreign keys enforced, then every read the app makes
of the org's AI setup is compared with a fresh org's.
"""
from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting, OrgAIFeatureRouting
from app.models.user import Organization
from app.services import ai_credential_service, ai_routing_service
from app.services.ai_dispatch import (
    NoRoutingConfigured,
    RoutedProviderNotSupported,
    call_llm,
    http_for_dispatch_error,
)
from tests.factories.ai import AI_ALL, ai_on_rows

_V = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_spec = importlib.util.spec_from_file_location("_m096", _V / "096_ai_provider_varchar.py")
m096 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m096)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _ai_setup(db: AsyncSession, org_id: int) -> dict:
    """Everything the app reads to decide what AI an org has."""
    try:
        await call_llm(
            db,
            org_id=org_id,
            feature_key="chat",
            request_payload={"messages": [{"role": "user", "content": "x"}]},
        )
    except NoRoutingConfigured as exc:
        dispatch = (type(exc), http_for_dispatch_error(exc).status_code, exc.code)
    else:  # pragma: no cover - the assertion below reports it
        dispatch = None
    return {
        "credentials": await ai_credential_service.list_credentials_for_org(db, org_id=org_id),
        "count": await ai_credential_service.count_credentials_for_org(db, org_id=org_id),
        "default": await ai_routing_service.get_default_routing(db, org_id=org_id),
        "features": await ai_routing_service.get_feature_routings(db, org_id=org_id),
        "route": await ai_routing_service.get_routing_for_feature(
            db, org_id=org_id, feature_name="chat"
        ),
        "dispatch": dispatch,
    }


@pytest.mark.asyncio
async def test_an_ollama_org_ends_up_like_an_org_without_a_provider(factory):
    async with factory() as db:
        ollama_org = Organization(name="Ollama", billing_cycle_day=1)
        fresh_org = Organization(name="Fresh", billing_cycle_day=1)
        other_org = Organization(name="OpenAI", billing_cycle_day=1)
        db.add_all([ollama_org, fresh_org, other_org])
        await db.flush()
        for org in (ollama_org, fresh_org, other_org):
            db.add_all(ai_on_rows(org.id, *AI_ALL))
        kept = OrgAICredential(org_id=other_org.id, provider=AiProvider.OPENAI, label="kept")
        db.add(kept)
        await db.flush()
        db.add(OrgAIDefaultRouting(org_id=other_org.id, credential_id=kept.id, model="gpt-4o"))
        # The ORM refuses a retired provider, so store it as an older release did.
        await db.execute(
            text(
                "INSERT INTO org_ai_credentials (org_id, provider, encrypted_bearer_token, "
                "base_url_is_api_root) VALUES (:o, 'ollama', 'token', 0)"
            ),
            {"o": ollama_org.id},
        )
        cred_id = (
            await db.execute(text("SELECT id FROM org_ai_credentials WHERE provider = 'ollama'"))
        ).scalar_one()
        db.add(OrgAIDefaultRouting(org_id=ollama_org.id, credential_id=cred_id, model="llama3"))
        db.add(OrgAIFeatureRouting(
            org_id=ollama_org.id, feature_name="chat", credential_id=cred_id, model="llama3"
        ))
        await db.commit()
        ids = (ollama_org.id, fresh_org.id, other_org.id)

    async with factory() as db:
        before = await _ai_setup(db, ids[0])
    # Before the migration the org is stuck on the inert row.
    assert before["dispatch"][0] is RoutedProviderNotSupported
    assert before["count"] == 1

    async with factory() as db:
        deleted = await db.run_sync(lambda s: m096.delete_retired_credentials(s.connection()))
        await db.commit()
    assert deleted == 1

    async with factory() as db:
        after = await _ai_setup(db, ids[0])
        fresh = await _ai_setup(db, ids[1])
        other_creds = await ai_credential_service.list_credentials_for_org(db, org_id=ids[2])
        other_route = await ai_routing_service.get_routing_for_feature(
            db, org_id=ids[2], feature_name="chat"
        )
        leftover = (await db.execute(text(
            "SELECT COUNT(*) FROM org_ai_credentials WHERE encrypted_bearer_token IS NOT NULL"
        ))).scalar_one()
    assert after == fresh
    assert after["dispatch"] == (NoRoutingConfigured, 412, "ai_routing_not_configured")
    assert after["credentials"] == [] and after["route"] is None
    assert leftover == 0
    # Another provider's credential and routing are untouched.
    assert [c.label for c in other_creds] == ["kept"]
    assert other_route == (other_creds[0].id, "gpt-4o")
