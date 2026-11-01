"""``org_ai_credentials.provider`` is VARCHAR validated against the provider
registry (TBD-584).

A stored value outside the registry (a retired provider such as ``ollama``)
loads as a plain string instead of raising, and can never be written again.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import String, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.org_ai_credential import (
    AiProvider,
    OrgAICredential,
    is_supported_provider,
    provider_key,
)
from app.models.user import Organization
from app.services.ai_providers import ProviderNotSupported, get_adapter


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


def test_registry_holds_no_retired_provider():
    assert {p.value for p in AiProvider} == {"openai", "anthropic", "openai_compatible"}


def test_column_is_varchar_32():
    col = OrgAICredential.__table__.c.provider
    assert isinstance(col.type.impl_instance, String)
    assert col.type.impl_instance.length == 32
    assert col.nullable is False


@pytest.mark.parametrize("value", ["ollama", "native", "OpenAI", ""])
def test_write_outside_the_registry_is_refused(value):
    with pytest.raises(ValueError):
        OrgAICredential(org_id=1, provider=value)


def test_write_coerces_a_registry_string_to_its_member():
    assert OrgAICredential(org_id=1, provider="openai").provider is AiProvider.OPENAI


async def test_retired_value_loads_as_a_plain_string(factory):
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(OrgAICredential(org_id=org.id, provider=AiProvider.ANTHROPIC))
        await db.execute(
            text("INSERT INTO org_ai_credentials (org_id, provider) VALUES (:o, 'ollama')"),
            {"o": org.id},
        )
        await db.commit()
    async with factory() as db:
        rows = (await db.execute(select(OrgAICredential).order_by(OrgAICredential.id))).scalars().all()
        stored = (await db.execute(text("SELECT provider FROM org_ai_credentials ORDER BY id"))).scalars().all()
    assert stored == ["anthropic", "ollama"]
    supported, retired = rows
    assert supported.provider is AiProvider.ANTHROPIC
    assert is_supported_provider(supported.provider)
    assert retired.provider == "ollama"
    assert not isinstance(retired.provider, AiProvider)
    assert not is_supported_provider(retired.provider)
    assert provider_key(retired.provider) == "ollama"
    assert provider_key(supported.provider) == "anthropic"


@pytest.mark.parametrize("provider", ["ollama", "native", "unknown"])
def test_get_adapter_refuses_a_retired_provider(provider):
    with pytest.raises(ProviderNotSupported) as exc_info:
        get_adapter(provider, api_key="sk-abcd", base_url="http://10.0.0.5:11434")
    assert exc_info.value.code == "ai_provider_not_supported"
