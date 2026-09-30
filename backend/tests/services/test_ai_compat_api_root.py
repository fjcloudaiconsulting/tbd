"""OpenAI-compatible base URL as the versioned API root (TBD-590).

Pins:
- F-G1a: a legacy row (``base_url_is_api_root`` False, every row created
  before TBD-590) issues byte-identical URLs: ``{base}/v1/<endpoint>``.
- F-G1b: a new row uses ``base_url`` as the API root, so the OpenRouter
  and Gemini presets reach ``.../chat/completions`` and ``.../models``.
- F-G1c: migration 083 leaves every pre-existing row at False.
- F-G2: ``get_adapter`` refuses an openai_compatible build without the
  flag; create/rotate/validate pass the row's flag.
- F-G3: new base URLs with a query, fragment or userinfo are refused.
- F-G4: the unversioned-root 404 hint (new rows only, path only).
- G-G5: Gemini stream usage and ``/models`` ids (hand-written fixture).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.user import Organization
from app.schemas.org_ai_credential import OrgAICredentialCreate
from app.services import ai_credential_service
from app.services.ai_credential_crypto import encrypt
from app.services.ai_providers import get_adapter
from app.services.ai_providers.openai_compatible import (
    OPENAI_COMPATIBLE_PRESETS,
    UNVERSIONED_ROOT_404_ERROR,
    OpenAICompatibleAdapter,
)

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = BACKEND / "tests" / "fixtures" / "ai_providers"
PRESET = {p["key"]: p["base_url"] for p in OPENAI_COMPATIBLE_PRESETS}

_CHAT = {
    "choices": [{"message": {"role": "assistant", "content": '{"a": 1}'}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}
_EMBED = {"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 2}}
_MODELS = {"data": [{"id": "m1"}]}
_SSE = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'


def _install(
    monkeypatch, *, status: int = 200, models: dict | None = None, sse: str = _SSE
) -> list[str]:
    """Answer every request by path; return the requested URLs in order."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        path = request.url.path
        if status != 200:
            return httpx.Response(status, json={})
        if path.endswith("/models"):
            return httpx.Response(200, json=models or _MODELS)
        if path.endswith("/embeddings"):
            return httpx.Response(200, json=_EMBED)
        if json.loads(request.content or b"{}").get("stream"):
            return httpx.Response(200, content=sse.encode())
        return httpx.Response(200, json=_CHAT)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return urls


async def _every_endpoint(adapter: OpenAICompatibleAdapter) -> None:
    msgs = [{"role": "user", "content": "x"}]
    assert (await adapter.validate()).ok
    await adapter.chat(model="m", messages=msgs)
    await adapter.embed(texts=["x"], model="e")
    await adapter.chat_structured(model="m", messages=msgs, schema={"type": "object"})
    await adapter.function_call(model="m", messages=msgs, tools=[])
    async for _ in adapter.stream(model="m", messages=msgs):
        pass


# ---------- F-G1a / F-G1b: request URLs -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "root"),
    [
        # The strings origin/main builds today, for rows it already holds.
        ("http://vllm.lan:8000", "http://vllm.lan:8000/v1"),
        ("https://api.together.xyz/", "https://api.together.xyz/v1"),
        ("https://api.groq.com/openai", "https://api.groq.com/openai/v1"),
        # Already doubled today (broken), and must stay exactly as it was.
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/v1"),
    ],
)
async def test_legacy_row_urls_are_byte_identical(monkeypatch, base_url, root):
    urls = _install(monkeypatch)
    await _every_endpoint(
        OpenAICompatibleAdapter(api_key="k", base_url=base_url, base_url_is_api_root=False)
    )
    assert urls == [
        f"{root}/models",
        f"{root}/chat/completions",
        f"{root}/embeddings",
        f"{root}/chat/completions",
        f"{root}/chat/completions",
        f"{root}/chat/completions",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["openrouter", "gemini"])
async def test_api_root_row_appends_only_the_endpoint(monkeypatch, preset):
    root = PRESET[preset]
    urls = _install(monkeypatch)
    await _every_endpoint(
        OpenAICompatibleAdapter(api_key="k", base_url=root + "/", base_url_is_api_root=True)
    )
    assert urls == [
        f"{root}/models",
        f"{root}/chat/completions",
        f"{root}/embeddings",
        f"{root}/chat/completions",
        f"{root}/chat/completions",
        f"{root}/chat/completions",
    ]


def test_presets_are_the_documented_api_roots():
    # A preset key equal to a provider key would shadow that provider's
    # option in the form's select.
    assert not set(PRESET) & {p.value for p in AiProvider}
    assert PRESET == {
        "openrouter": "https://openrouter.ai/api/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    }


# ---------- F-G2: builders ------------------------------------------


def test_get_adapter_refuses_compat_build_without_the_flag():
    with pytest.raises(ValueError, match="base_url_is_api_root"):
        get_adapter(AiProvider.OPENAI_COMPATIBLE, api_key="k", base_url="https://h/v1")


@pytest.mark.parametrize("flag", [True, False])
def test_get_adapter_passes_the_flag(flag):
    adapter = get_adapter(
        AiProvider.OPENAI_COMPATIBLE,
        api_key="k",
        base_url="https://h",
        base_url_is_api_root=flag,
    )
    assert adapter.api_root == ("https://h" if flag else "https://h/v1")


# ---------- F-G4: unversioned-root 404 hint ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "is_api_root", "status", "error"),
    [
        ("https://h", True, 404, UNVERSIONED_ROOT_404_ERROR),
        # A version-like host is not a version path.
        ("https://v1", True, 404, UNVERSIONED_ROOT_404_ERROR),
        ("https://h/v1", True, 404, "Provider rejected the request (404)"),
        ("https://h/v1beta/openai", True, 404, "Provider rejected the request (404)"),
        # Legacy rows keep today's message.
        ("https://h", False, 404, "Provider rejected the request (404)"),
        ("https://h", True, 401, "Provider rejected the request (401)"),
    ],
)
async def test_unversioned_root_404_hint(monkeypatch, base_url, is_api_root, status, error):
    _install(monkeypatch, status=status)
    result = await OpenAICompatibleAdapter(
        api_key="k", base_url=base_url, base_url_is_api_root=is_api_root
    ).validate()
    assert result.ok is False
    assert result.error == error


# ---------- F-G3: new base URLs -------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://h.example.com/v1?api-version=1",
        "https://h.example.com/v1#frag",
        "https://h.example.com/v1?",
        "https://user:pw@h.example.com/v1",
        "https://user@h.example.com/v1",
    ],
)
def test_create_refuses_query_fragment_and_userinfo(base_url):
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE, api_key="sk-12345678", base_url=base_url
        )


# Guard: the flag is server-written only; extra="forbid" keeps it out.
def test_create_refuses_a_client_supplied_flag():
    with pytest.raises(ValidationError):
        OrgAICredentialCreate(
            provider=AiProvider.OPENAI_COMPATIBLE,
            api_key="sk-12345678",
            base_url="https://h.example.com/v1",
            base_url_is_api_root=False,
        )


def test_ollama_keeps_userinfo_for_basic_auth(monkeypatch):
    monkeypatch.setattr(app_settings, "ai_provider_allow_private_networks", False)
    OrgAICredentialCreate(provider=AiProvider.OLLAMA, base_url="https://u:p@nas.example.com:11434")


# ---------- G-G5: Gemini fixture ------------------------------------


@pytest.mark.asyncio
async def test_gemini_stream_usage_and_model_ids(monkeypatch):
    fixture = json.loads((FIXTURES / "gemini_openai_compat.json").read_text())
    adapter = OpenAICompatibleAdapter(
        api_key="k", base_url=PRESET["gemini"], base_url_is_api_root=True
    )

    _install(monkeypatch, models=fixture["models"], sse=fixture["stream_sse"])
    result = await adapter.validate()
    # Ids are kept verbatim, models/ prefix included.
    assert result.discovered_models == [m["id"] for m in fixture["models"]["data"]]

    chunks = [
        c
        async for c in adapter.stream(
            model="gemini-2.5-flash", messages=[{"role": "user", "content": "x"}]
        )
    ]
    assert "".join(c.delta_text for c in chunks) == "Hello"
    # Gemini puts usage on the last content chunk, not an empty-choices one.
    assert chunks[-1].done
    assert (chunks[-1].final_usage.prompt_tokens, chunks[-1].final_usage.completion_tokens) == (5, 2)


# ---------- F-G2 service paths, F-G1c migration -----------------------


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")


_ACTOR_KW = {
    "actor_user_id": 1,
    "actor_email": "actor@example.test",
    "request_id": "rid-test",
    "ip_address": "127.0.0.1",
}


@pytest.mark.asyncio
async def test_create_stores_api_root_and_validates_at_preset(monkeypatch, session_factory):
    urls = _install(monkeypatch)
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        row = await ai_credential_service.create_credential(
            db,
            org_id=org.id,
            payload=OrgAICredentialCreate(
                provider=AiProvider.OPENAI_COMPATIBLE,
                api_key="sk-or-12345678",
                base_url=PRESET["openrouter"],
            ),
            session_factory=session_factory,
            **_ACTOR_KW,
        )
    assert row.base_url_is_api_root is True
    assert urls == ["https://openrouter.ai/api/v1/models"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rotate", "validate"])
async def test_legacy_row_rotate_and_validate_keep_the_v1_root(
    monkeypatch, session_factory, action
):
    urls = _install(monkeypatch)
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        # A row as it exists before TBD-590: the flag is not set and reads
        # False (migration 083's default is fenced by the migration test).
        row = OrgAICredential(
            org_id=org.id,
            provider=AiProvider.OPENAI_COMPATIBLE,
            encrypted_api_key=encrypt("sk-legacy-1234"),
            base_url="http://vllm.lan:8000",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        assert row.base_url_is_api_root is False
        if action == "rotate":
            result = await ai_credential_service.rotate_credential(
                db,
                credential=row,
                new_api_key="sk-rotated-9999",
                new_bearer_token=None,
                session_factory=session_factory,
                **_ACTOR_KW,
            )
        else:
            result = await ai_credential_service.validate_credential(
                db, credential=row, session_factory=session_factory, **_ACTOR_KW
            )
    assert urls == ["http://vllm.lan:8000/v1/models"]
    assert result.base_url_is_api_root is False
    assert result.validation_error is None


def _load_migration_083():
    path = BACKEND / "alembic" / "versions" / "083_ai_credential_api_root.py"
    spec = importlib.util.spec_from_file_location("m083", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_083_leaves_existing_rows_legacy():
    m083 = _load_migration_083()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE org_ai_credentials ("
                "id INTEGER PRIMARY KEY, provider VARCHAR(32), base_url VARCHAR(512))"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO org_ai_credentials (id, provider, base_url) "
                "VALUES (1, 'openai_compatible', 'http://vllm.lan:8000')"
            )
        )
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            m083.upgrade()
        assert conn.execute(
            sa.text("SELECT base_url_is_api_root, base_url FROM org_ai_credentials")
        ).one() == (0, "http://vllm.lan:8000")
        with Operations.context(ctx):
            m083.downgrade()
        cols = [c["name"] for c in sa.inspect(conn).get_columns("org_ai_credentials")]
        assert "base_url_is_api_root" not in cols
