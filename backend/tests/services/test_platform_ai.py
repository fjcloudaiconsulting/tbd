"""TBD-586 PR1 stage 1: platform settings reader, adapter factory (C4),
fixed capability sets, config keys, mcp_main refusal (F-N3), reserved prefix."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.services import platform_ai, platform_ai_settings
from app.services.ai_pricing import MODEL_PRICING
from app.services.ai_providers import NativeNotAvailable
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter

APP = Path(__file__).resolve().parents[2] / "app"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _set(db, **kv):
    for k, v in kv.items():
        db.add(SystemSetting(key=f"platform_ai.{k}", value=v))
    await db.commit()


# ---- settings reader -----------------------------------------------------

async def test_settings_absent_means_off_zero_empty(db):
    s = await platform_ai_settings.load(db)
    assert (s.enabled, s.global_monthly_cents, s.models) == (False, 0, {})


async def test_settings_parsed(db):
    await _set(db, enabled="on", global_monthly_cents="500", models='{"openai": ["gpt-4o"]}')
    s = await platform_ai_settings.load(db)
    assert (s.enabled, s.global_monthly_cents, s.models) == (True, 500, {"openai": ["gpt-4o"]})


@pytest.mark.parametrize("enabled,cents,models", [
    ("yes", "abc", "not json"),
    ("", "-5", "[1,2]"),
    ("ON ", "1.5", '{"openai": "gpt-4o"}'),
    ("true", "", '{"openai": [1, null]}'),
])
async def test_settings_malformed_fails_closed(db, enabled, cents, models):
    await _set(db, enabled=enabled, global_monthly_cents=cents, models=models)
    s = await platform_ai_settings.load(db)
    assert s.enabled is False
    assert s.global_monthly_cents == 0
    assert s.models.get("openai", []) == []


async def test_settings_read_system_setting_only(db):
    src = (APP / "services/platform_ai_settings.py").read_text()
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "OrgSetting" not in names and "SystemSetting" in names


# ---- factory (C4) --------------------------------------------------------

@pytest.fixture
def keys(monkeypatch):
    for p in platform_ai.PLATFORM_PROVIDERS:
        monkeypatch.setattr(app_settings, f"platform_ai_{p}_api_key", f"env-{p}")


def test_factory_derives_everything_from_platform_provider(keys):
    a = platform_ai.build_adapter("openrouter")
    assert isinstance(a, OpenAICompatibleAdapter)
    assert (a.api_key, a.api_root) == ("env-openrouter", "https://openrouter.ai/api/v1")
    assert a.base_url_is_api_root is True
    assert a.extra_body == {"provider": {"data_collection": "deny"}}
    g = platform_ai.build_adapter("gemini")
    assert isinstance(g, OpenAICompatibleAdapter)
    assert (g.api_key, g.api_root) == (
        "env-gemini", "https://generativelanguage.googleapis.com/v1beta/openai")
    assert g.extra_body == {}
    o = platform_ai.build_adapter("openai")
    assert isinstance(o, OpenAIAdapter) and o.api_key == "env-openai"
    n = platform_ai.build_adapter("anthropic")
    assert isinstance(n, AnthropicAdapter) and n.api_key == "env-anthropic"


def test_factory_missing_key_is_native_not_available(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_ai_openai_api_key", "")
    with pytest.raises(NativeNotAvailable):
        platform_ai.build_adapter("openai")


def test_factory_never_decrypts_or_reads_stored_columns():
    tree = ast.parse((APP / "services/platform_ai.py").read_text())
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not used & {"decrypt", "encrypted_api_key", "base_url_is_api_root_col", "get_adapter"}
    assert "encrypted_bearer_token" not in used


def test_anthropic_capability_set_excludes_embed():
    caps = platform_ai.PLATFORM_CAPABILITIES
    assert "embed" not in caps["anthropic"]
    for p in ("openrouter", "openai", "gemini"):
        assert set(caps[p]) == {"chat", "embed", "structured_output", "function_call", "stream"}
    assert set(caps["anthropic"]) == {"chat", "structured_output", "function_call", "stream"}


# ---- C12 forcing test ----------------------------------------------------

def test_c12_every_priced_openai_chat_model_is_in_the_max_tokens_set():
    """A priced OpenAI chat model that is not vetted for ``max_tokens`` (e.g. a
    reasoning model that needs ``max_completion_tokens``) fails here until a
    human decides. OpenRouter ``openai/*`` ids go through another path."""
    openai_chat = {
        m for m in MODEL_PRICING
        if m != "_default" and not m.startswith(("claude-", "text-embedding-"))
    }
    assert openai_chat <= platform_ai.OPENAI_ACCEPTS_MAX_TOKENS


# ---- config + mcp_main refusal (F-N3) ------------------------------------

def test_config_has_four_platform_keys_default_empty():
    for p in ("openrouter", "openai", "anthropic", "gemini"):
        assert type(app_settings).model_fields[f"platform_ai_{p}_api_key"].default == ""


def _import_mcp_main(extra_env: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env}
    return subprocess.run(
        [sys.executable, "-c", "import app.mcp_main"],
        cwd=str(APP.parent), env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("p", ["OPENROUTER", "OPENAI", "ANTHROPIC", "GEMINI"])
def test_f_n3_mcp_main_refuses_to_boot_with_a_platform_key(p):
    r = _import_mcp_main({f"PLATFORM_AI_{p}_API_KEY": "k"})
    assert r.returncode != 0
    assert f"PLATFORM_AI_{p}_API_KEY" in r.stderr


def test_f_n3_control_mcp_main_boots_without_keys():
    env = {f"PLATFORM_AI_{p}_API_KEY": "" for p in ("OPENROUTER", "OPENAI", "ANTHROPIC", "GEMINI")}
    r = _import_mcp_main(env)
    assert r.returncode == 0, r.stderr[-800:]


# ---- reserved settings prefix --------------------------------------------

def test_reserved_prefix_blocks_platform_ai_in_the_generic_writer():
    from app.routers import settings as sr
    assert "platform_ai." in sr.RESERVED_SETTINGS_PREFIX
    assert "Platform_AI.enabled".casefold().startswith(sr.RESERVED_SETTINGS_PREFIX)
    assert "platform_ai." in sr._RESERVED_NAMESPACE_DETAIL


# ---- F-S2: an org-scoped platform_ai.* is refused at write, ignored at read --

@pytest.mark.parametrize("key", ["platform_ai.enabled", "Platform_AI.enabled", "PLATFORM_AI.models"])
def test_f_s2_generic_writer_refuses_the_namespace_in_any_case(key):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.deps import get_current_user
    from app.models.user import Role
    from app.routers.settings import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, org_id=1, role=Role.ADMIN, is_superadmin=False)
    app.dependency_overrides[get_db] = lambda: None  # refused before any query
    with TestClient(app) as c:
        put = c.put("/api/v1/settings", json={"key": key, "value": "on"})
        assert put.status_code == 403 and "platform_ai." in put.json()["detail"]
        assert c.delete(f"/api/v1/settings/{key}").status_code == 403


async def test_f_s2_reader_ignores_an_org_scoped_row(db):
    from app.models.user import Organization

    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    db.add(OrgSetting(org_id=org.id, key="platform_ai.enabled", value="on"))
    db.add(OrgSetting(org_id=org.id, key="platform_ai.global_monthly_cents", value="999"))
    await db.commit()
    s = await platform_ai_settings.load(db)
    assert (s.enabled, s.global_monthly_cents) == (False, 0)
