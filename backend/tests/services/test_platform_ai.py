"""TBD-586 PR1 stage 1: platform settings reader, adapter factory (C4),
fixed capability sets, config keys, mcp_main refusal (F-N3), reserved prefix."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.platform_ai_model import PlatformAIModel
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.services import platform_ai, platform_ai_settings
from app.services.ai_providers import NativeNotAvailable
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

async def test_settings_absent_means_off_zero(db):
    s = await platform_ai_settings.load(db)
    assert (s.enabled, s.global_monthly_cents) == (False, 0)


async def test_settings_parsed(db):
    await _set(db, enabled="on", global_monthly_cents="500", models='{"openai": ["gpt-4o"]}')
    s = await platform_ai_settings.load(db)
    assert (s.enabled, s.global_monthly_cents) == (True, 500)


@pytest.mark.parametrize("enabled,cents", [("yes", "abc"), ("", "-5"), ("ON ", "1.5"), ("true", "")])
async def test_settings_malformed_fails_closed(db, enabled, cents):
    await _set(db, enabled=enabled, global_monthly_cents=cents)
    s = await platform_ai_settings.load(db)
    assert s.enabled is False
    assert s.global_monthly_cents == 0


async def test_settings_read_system_setting_only(db):
    src = (APP / "services/platform_ai_settings.py").read_text()
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "OrgSetting" not in names and "SystemSetting" in names


# ---- factory (C4) --------------------------------------------------------

@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", "env-openrouter")


def _build(provider="openrouter"):
    return platform_ai.build_adapter(provider, models=["a/m"], max_price=(1, 1))


def test_fence17_factory_is_openrouter_only(keys, monkeypatch):
    """FENCE 17. Kills a surviving direct-vendor branch."""
    a = _build()
    assert isinstance(a, OpenAICompatibleAdapter)
    assert (a.api_key, a.api_root) == ("env-openrouter", "https://openrouter.ai/api/v1")
    assert a.base_url_is_api_root is True
    for p in ("openai", "anthropic", "gemini"):
        with pytest.raises(NativeNotAvailable):
            _build(p)
    monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", "")
    with pytest.raises(NativeNotAvailable):
        _build()


async def test_fence36_platform_adapter_refuses_embed(keys, monkeypatch):
    """FENCE 36. Kills a platform embed reaching OpenRouter without the privacy prefs."""
    reqs = []
    transport = httpx.MockTransport(lambda r: reqs.append(r) or httpx.Response(200, json={}))
    orig = httpx.AsyncClient.__init__

    def _init(self, *a, **kw):
        kw["transport"] = transport
        orig(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    with pytest.raises(NativeNotAvailable):
        await _build().embed(texts=["hi"], model="a/m")
    assert reqs == []


def test_factory_never_decrypts_or_reads_stored_columns():
    tree = ast.parse((APP / "services/platform_ai.py").read_text())
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not used & {"decrypt", "encrypted_api_key", "base_url_is_api_root_col", "get_adapter"}
    assert "encrypted_bearer_token" not in used


def _row(*, structured=True, ctx=128_000, mct=4096):
    return PlatformAIModel(openrouter_id="a/m", name="M", context_length=ctx, max_completion_tokens=mct,
                           supports_structured_outputs=structured)


def test_fence19_can_reads_per_model_capability():
    """FENCE 19. Kills per-model capability ignored."""
    assert all(platform_ai.can(_row(), c) for c in ("chat", "structured_output", "function_call", "stream"))
    assert platform_ai.can(_row(structured=False), "structured_output") is False
    assert platform_ai.can(_row(structured=False), "function_call") is True
    assert platform_ai.can(_row(), "embed") is False
    assert platform_ai.can(_row(ctx=127_999), "chat") is False
    assert platform_ai.can(_row(mct=4095), "chat") is False
    assert platform_ai.can(_row(mct=None), "chat") is False


_KEY = "platform_ai_openrouter_api_key"
_KEY_READERS = {("services/platform_ai.py", "build_adapter"),
                ("services/platform_ai.py", "platform_key_configured"),
                ("mcp_main.py", "refuse_platform_ai_keys")}


def _is_settings(node) -> bool:
    return (isinstance(node, ast.Name) and node.id in ("settings", "app_settings")) or (
        isinstance(node, ast.Attribute) and node.attr == "settings")


def test_fence35_the_house_key_never_leaves_its_three_readers():
    """FENCE 35. Kills a helper that hands the key string out; the dynamic
    ``getattr`` read a name-based fence cannot see."""
    seen = set()
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel == "config.py":
            continue
        tree = ast.parse(path.read_text())
        fns = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    fns.setdefault(n, fn.name)  # outermost first: ast.walk is BFS
        for n in ast.walk(tree):
            hit = ((isinstance(n, ast.Attribute) and n.attr == _KEY)
                   or (isinstance(n, ast.Constant) and n.value == _KEY))
            if hit:
                assert (rel, fns.get(n)) in _KEY_READERS, (rel, n.lineno)
                seen.add((rel, fns.get(n)))
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            on_settings = bool(n.args) and _is_settings(n.args[0])
            # getattr(settings, <anything but a literal>): f-string, %, .format, +, a variable
            if name == "getattr" and on_settings and not (
                len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)
            ):
                assert (rel, fns.get(n)) == ("mcp_main.py", "refuse_platform_ai_keys"), (rel, n.lineno)
            # whole-object dumps
            assert not (name in ("dict", "vars") and on_settings), (rel, n.lineno)
            assert not (isinstance(f, ast.Attribute) and _is_settings(f.value)
                        and f.attr in ("model_dump", "model_dump_json", "dict", "json")), (rel, n.lineno)
    assert seen == _KEY_READERS
    assert not hasattr(platform_ai, "platform_key")


def test_fence35_key_configured_is_a_bool(keys):
    assert platform_ai.platform_key_configured("openrouter") is True
    assert platform_ai.platform_key_configured("openai") is False


# ---- config + mcp_main refusal (F-N3) ------------------------------------

def test_fence18_config_has_only_the_openrouter_key():
    """FENCE 18. Kills dead keys."""
    fields = type(app_settings).model_fields
    assert fields["platform_ai_openrouter_api_key"].default == ""
    for p in ("openai", "anthropic", "gemini"):
        assert f"platform_ai_{p}_api_key" not in fields


def _import_mcp_main(extra_env: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env}
    return subprocess.run(
        [sys.executable, "-c", "import app.mcp_main"],
        cwd=str(APP.parent), env=env, capture_output=True, text=True, timeout=120,
    )


def test_f_n3_mcp_main_refuses_to_boot_with_a_platform_key():
    """FENCE 18. Kills the mcp guard lost with the list."""
    p = "OPENROUTER"
    r = _import_mcp_main({f"PLATFORM_AI_{p}_API_KEY": "k"})
    assert r.returncode != 0
    # Named in the JSON line that logs the refusal; the crash hook itself prints the class only.
    assert f'"variable": "PLATFORM_AI_{p}_API_KEY"' in r.stdout
    assert "RuntimeError" in r.stdout


def test_f_n3_control_mcp_main_boots_without_keys():
    env = {"PLATFORM_AI_OPENROUTER_API_KEY": ""}
    r = _import_mcp_main(env)
    assert r.returncode == 0, r.stderr[-800:]


# ---- reserved settings prefix --------------------------------------------

def test_reserved_prefix_blocks_platform_ai_in_the_generic_writer():
    from app.routers import settings as sr
    assert "platform_ai." in sr.RESERVED_SETTINGS_PREFIX
    assert "Platform_AI.enabled".casefold().startswith(sr.RESERVED_SETTINGS_PREFIX)
    assert "platform_ai." in sr._RESERVED_NAMESPACE_DETAIL


# ---- F-S2: an org-scoped platform_ai.* is refused at write, ignored at read --

@pytest.mark.parametrize("key", ["platform_ai.enabled", "Platform_AI.enabled", "PLATFORM_AI.global_monthly_cents"])
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
