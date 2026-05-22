"""Per-adapter ``validate()`` capability advertising.

Blocker fix: the validate() probe must reflect what each provider /
model actually supports, otherwise the dispatch capability gate at
``_prepare_dispatch`` rejects every call beyond the legacy
``chat``/``embed`` pair even when real credentials are present.

Rules pinned here:
- OpenAI: chat, embed, function_call, stream always; structured_output
  only when the discovered model list includes a json_schema-capable
  prefix (gpt-4o, gpt-4o-mini, gpt-4o-2024-08-06, gpt-4.1, gpt-5).
- Anthropic: chat, structured_output, function_call, stream — no
  embed (Anthropic has no embeddings API).
- Ollama: chat, embed, stream always; function_call + structured_output
  conditional on any discovered model matching the
  ``KNOWN_FUNCTION_CALL_MODELS`` prefix allowlist.
- OpenAI-compatible: chat, embed, structured_output, function_call,
  stream — the full surface this adapter implements. The /v1/models
  response carries no capability metadata, so we trust the
  user-configured endpoint and rely on the sanitized provider error
  path (PR1) when the underlying server doesn't honor a request.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai_providers import anthropic as anthropic_mod
from app.services.ai_providers import ollama as ollama_mod
from app.services.ai_providers import openai as openai_mod
from app.services.ai_providers import openai_compatible as oai_compat_mod
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter


_RealAsyncClient = httpx.AsyncClient


def _patch_models_response(monkeypatch, module, payload: dict) -> None:
    """Replace ``module.httpx.AsyncClient`` with a transport that
    returns the supplied JSON payload for the /models GET call.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(payload))

    def _client(*_args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _client)


# ---------- OpenAI ----------------------------------------------------


@pytest.mark.asyncio
async def test_openai_validate_advertises_full_capability_set(monkeypatch):
    """OpenAI key with access to gpt-4o-mini → chat, embed, stream,
    function_call, structured_output. Pins the json_schema gate hits
    when at least one capable model is present.
    """
    _patch_models_response(
        monkeypatch,
        openai_mod,
        {
            "data": [
                {"id": "gpt-4o-mini"},
                {"id": "text-embedding-3-small"},
                {"id": "gpt-3.5-turbo"},
            ]
        },
    )
    adapter = OpenAIAdapter(api_key="sk-test")
    result = await adapter.validate()
    assert result.ok is True
    caps = set(result.discovered_capabilities)
    assert caps == {
        "chat",
        "embed",
        "function_call",
        "stream",
        "structured_output",
    }


@pytest.mark.asyncio
async def test_openai_validate_without_json_schema_model_omits_structured(
    monkeypatch,
):
    """A legacy-only OpenAI key (gpt-3.5-turbo, no gpt-4o family) still
    gets chat/embed/function_call/stream, but ``structured_output`` is
    NOT advertised — the json_schema response_format requires the 2024-08
    family or newer.
    """
    _patch_models_response(
        monkeypatch,
        openai_mod,
        {"data": [{"id": "gpt-3.5-turbo"}]},
    )
    adapter = OpenAIAdapter(api_key="sk-test")
    result = await adapter.validate()
    assert result.ok is True
    caps = set(result.discovered_capabilities)
    assert caps == {"chat", "embed", "function_call", "stream"}
    assert "structured_output" not in caps


# ---------- Anthropic -------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_validate_advertises_chat_structured_function_stream(
    monkeypatch,
):
    """Anthropic exposes chat + tool use + structured output + streaming.
    Embed is intentionally absent (no embeddings API).
    """
    _patch_models_response(
        monkeypatch,
        anthropic_mod,
        {"data": [{"id": "claude-3-5-sonnet-20241022"}]},
    )
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    result = await adapter.validate()
    assert result.ok is True
    caps = set(result.discovered_capabilities)
    assert caps == {"chat", "structured_output", "function_call", "stream"}
    assert "embed" not in caps


# ---------- Ollama ----------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_validate_with_function_capable_model_advertises_full(
    monkeypatch,
):
    """An Ollama install whose model list intersects with
    ``KNOWN_FUNCTION_CALL_MODELS`` (e.g. llama3.1) advertises chat,
    embed, stream + function_call + structured_output.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "models": [
                        {"name": "llama3.1:8b"},
                        {"name": "nomic-embed-text"},
                    ]
                }
            ),
        )

    def _client(*_args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(ollama_mod.httpx, "AsyncClient", _client)

    adapter = OllamaAdapter(base_url="http://localhost:11434", api_key="")
    result = await adapter.validate()
    assert result.ok is True
    caps = set(result.discovered_capabilities)
    assert caps == {
        "chat",
        "embed",
        "stream",
        "function_call",
        "structured_output",
    }


@pytest.mark.asyncio
async def test_ollama_validate_with_non_function_capable_model_omits_function(
    monkeypatch,
):
    """An Ollama install with only legacy models (no llama3.1+,
    mistral-nemo, etc.) advertises just the baseline:
    chat, embed, stream.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps({"models": [{"name": "llama2:7b"}]}),
        )

    def _client(*_args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(ollama_mod.httpx, "AsyncClient", _client)

    adapter = OllamaAdapter(base_url="http://localhost:11434", api_key="")
    result = await adapter.validate()
    assert result.ok is True
    caps = set(result.discovered_capabilities)
    assert caps == {"chat", "embed", "stream"}
    assert "function_call" not in caps
    assert "structured_output" not in caps


# ---------- OpenAI-compatible ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_advertises_all_five_capabilities(
    monkeypatch,
):
    """OpenAI-compatible endpoints (vLLM, llama.cpp, LM Studio, hosted
    third parties) advertise the full capability surface this adapter
    implements: chat, embed, structured_output, function_call, stream.

    The /v1/models response carries no capability metadata, so we can't
    introspect individual support. Instead, the adapter trusts the
    user-configured endpoint (they typed in its URL) and falls back on
    the sanitized error path (PR1 contract) when the underlying server
    doesn't honor a particular request.
    """
    _patch_models_response(
        monkeypatch,
        oai_compat_mod,
        {"data": [{"id": "mixtral-8x7b"}, {"id": "embed-large"}]},
    )
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test", base_url="https://api.example.com"
    )
    result = await adapter.validate()
    assert result.ok is True
    caps = set(result.discovered_capabilities)
    assert caps == {
        "chat",
        "embed",
        "structured_output",
        "function_call",
        "stream",
    }


@pytest.mark.asyncio
async def test_openai_compatible_default_capabilities_constant(monkeypatch):
    """Module-level DEFAULT_CAPABILITIES pins the full 5-cap set so
    accidental edits to the constant fail loudly.
    """
    assert set(oai_compat_mod.DEFAULT_CAPABILITIES) == {
        "chat",
        "embed",
        "structured_output",
        "function_call",
        "stream",
    }
