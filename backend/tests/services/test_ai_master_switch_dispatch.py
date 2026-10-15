"""TBD-627 F1-F4, F13: the org AI master switch inside ``_prepare_dispatch``.

BYOK-routed org, real dispatch, adapter and decrypt are spies. Every refusal
must land BEFORE ``decrypt``/``get_adapter`` and before any ledger row.
"""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.org_ai_consent import OrgAIConsent
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import ROUTABLE_FEATURE_NAMES, OrgAIDefaultRouting
from app.models.user import Organization
from app.services import ai_dispatch
from app.services.ai_credential_crypto import encrypt
from app.services.ai_dispatch import PlatformConsentRequired, http_for_dispatch_error
from app.services.ai_feature_map import AI_FEATURE_MAP
from app.services.ai_providers.base import LLMResponse
from tests.factories.ai import ai_on_rows

MSGS = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
SCHEMA = {"type": "object", "required": ["a"]}
ALL_CAPS = ["chat", "embed", "function_call", "structured_output", "stream"]
ALL_AI = [e for e, _, _ in AI_FEATURE_MAP]


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk(dbapi_conn, _r):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(sf):
    async with sf() as s:
        yield s


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")


@pytest.fixture
def spies():
    """Adapter and decrypt spies; a refusal must leave both untouched."""
    adapter = MagicMock()
    with patch("app.services.ai_dispatch.get_adapter", return_value=adapter) as ga, \
         patch("app.services.ai_dispatch.decrypt", return_value="k") as dec:
        yield MagicMock(get_adapter=ga, decrypt=dec, adapter=adapter)


async def mk_byok(db, *, caps=None, consent=True, features=ALL_AI, routing_names=()):
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    cred = OrgAICredential(
        org_id=org.id, provider=AiProvider.OPENAI, encrypted_api_key=encrypt("sk-x"),
        key_fingerprint="0123456789abcdef", last_four="2345", label="p",
        discovered_capabilities=ALL_CAPS if caps is None else caps,
    )
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=org.id, credential_id=cred.id, model="gpt-4o-mini"))
    rows = ai_on_rows(org.id, *features)
    db.add_all(rows if consent else rows[1:])
    await db.commit()
    return org.id


async def _chat(db, org, fk="chat"):
    return await ai_dispatch.call_llm(db, org_id=org, feature_key=fk, request_payload={"messages": MSGS})


async def _structured(db, org, fk="chat"):
    return await ai_dispatch.call_llm_structured(db, org_id=org, feature_key=fk, messages=MSGS, response_schema=SCHEMA)


async def _function(db, org, fk="chat"):
    return await ai_dispatch.call_llm_function(db, org_id=org, feature_key=fk, messages=MSGS, tools=TOOLS)


async def _stream(db, org, fk="chat"):
    return [c async for c in ai_dispatch.call_llm_stream(db, org_id=org, feature_key=fk, messages=MSGS)]


async def _embed(db, org, fk="chat"):
    return await ai_dispatch.call_llm_embed(db, org_id=org, feature_key=fk, texts=["hi"], model="text-embedding-3-small")


ENTRIES = {"call_llm": _chat, "structured": _structured, "function": _function, "stream": _stream, "embed": _embed}
ALL = pytest.mark.parametrize("entry", list(ENTRIES))


async def _ledger(db):
    return (await db.scalars(select(AIUsageLedger))).all()


async def _break_consent(db, org, how):
    if how == "no_row":
        await db.execute(OrgAIConsent.__table__.delete().where(OrgAIConsent.org_id == org))
    elif how == "revoked":
        from app._time import utcnow_naive
        db.add(OrgAIConsent(org_id=org, consent_version=app_settings.ai_native_current_consent_version,
                            revoked_at=utcnow_naive()))
    else:
        db.add(OrgAIConsent(org_id=org, consent_version="old-version"))
    await db.commit()


# ---- F1 ---------------------------------------------------------------------

@ALL
@pytest.mark.parametrize("how", ["no_row", "revoked", "version_bump"])
async def test_f1_byok_org_without_current_consent_is_refused_before_decrypt(db, spies, entry, how):
    org = await mk_byok(db)
    await _break_consent(db, org, how)
    with pytest.raises(PlatformConsentRequired) as ei:
        await ENTRIES[entry](db, org)
    spies.get_adapter.assert_not_called()
    spies.decrypt.assert_not_called()
    assert await _ledger(db) == []
    detail = http_for_dispatch_error(ei.value).detail
    assert detail["code"] == "ai_consent_required"
    assert "current_consent_version" not in detail  # members see this body
    assert detail["message"] == (
        "AI is off for your organization. An admin can turn it on in Settings, AI providers."
    )


# ---- F2 ---------------------------------------------------------------------

async def test_f2_consent_is_checked_before_the_capability_check(db, spies):
    org = await mk_byok(db, caps=["chat"], consent=False)
    with pytest.raises(PlatformConsentRequired):
        await ai_dispatch.call_llm_function(db, org_id=org, feature_key="chat", messages=MSGS, tools=TOOLS)
    spies.decrypt.assert_not_called()
    spies.get_adapter.assert_not_called()


# ---- F3 ---------------------------------------------------------------------

@pytest.mark.parametrize("ent,routing,_ui", AI_FEATURE_MAP)
async def test_f3a_each_routing_name_needs_its_ai_entitlement(db, spies, ent, routing, _ui):
    org = await mk_byok(db, features=[e for e in ALL_AI if e != ent])
    with pytest.raises(ai_dispatch.AIFeatureNotEntitled) as ei:
        await _chat(db, org, routing)
    exc = http_for_dispatch_error(ei.value)
    assert exc.status_code == 403
    assert exc.detail == {"code": "feature_not_enabled", "feature_key": ent}
    spies.get_adapter.assert_not_called()
    spies.decrypt.assert_not_called()


@pytest.mark.parametrize("ent,routing,_ui", AI_FEATURE_MAP)
async def test_f3a_with_the_key_on_it_dispatches(db, spies, ent, routing, _ui):
    org = await mk_byok(db, features=[ent])
    spies.adapter.chat = AsyncMock(return_value=LLMResponse(
        content="x", prompt_tokens=1, completion_tokens=1, model="gpt-4o-mini"))
    await _chat(db, org, routing)
    spies.adapter.chat.assert_awaited_once()


async def test_f3b_unmapped_feature_key_fails_closed(db, spies):
    org = await mk_byok(db)  # default routing row, consent on, every ai.* on
    with pytest.raises(ai_dispatch.AIFeatureNotEntitled):
        await _chat(db, org, "not_a_mapped_feature")
    spies.get_adapter.assert_not_called()
    spies.decrypt.assert_not_called()


# ---- F4 ---------------------------------------------------------------------

def test_f4_every_routable_feature_has_an_entitlement_mapping():
    assert {r for _, r, _ in AI_FEATURE_MAP} == set(ROUTABLE_FEATURE_NAMES)
