"""Provider response text MUST NOT leak into ValidateResult.error.

Pins the architect's security fix — the OAI-compatible endpoint is the
worst-case offender (it's arbitrary user-controlled HTTP), but every
adapter is fixed uniformly so a future bug in one place doesn't silently
exfiltrate the just-submitted API key.

The test feeds a fake response whose body LOOKS like it echoed the
request (a common provider-debug pattern); we assert the API key /
secret-shaped strings do not survive into the error field.
"""
from __future__ import annotations

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


HOSTILE_BODY = (
    "Unauthorized. Your request was: api_key=secret123 "
    "Authorization: Bearer sk-LEAKED-456 "
    "Origin: https://internal.example.com/?token=hunter2"
)

_RealAsyncClient = httpx.AsyncClient


def _mock_transport(status: int, body: str) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def _patch_with_mock(monkeypatch, module, status: int, body: str) -> None:
    def _client(*_args, **kwargs):
        kwargs["transport"] = _mock_transport(status, body)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _client)


def _patch_with_exc(monkeypatch, module, exc_factory) -> None:
    def _client(*_args, **kwargs):
        def handler(_req: httpx.Request) -> httpx.Response:
            raise exc_factory()

        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _client)


@pytest.mark.asyncio
async def test_openai_compatible_sanitizes_4xx_response(monkeypatch):
    adapter = OpenAICompatibleAdapter(
        api_key="sk-LEAKED-456", base_url="https://api.example.com"
    )
    _patch_with_mock(monkeypatch, oai_compat_mod, 401, HOSTILE_BODY)
    result = await adapter.validate()
    assert result.ok is False
    assert "secret123" not in (result.error or "")
    assert "LEAKED" not in (result.error or "")
    assert "hunter2" not in (result.error or "")
    assert "internal.example.com" not in (result.error or "")
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_openai_compatible_sanitizes_5xx_response(monkeypatch):
    adapter = OpenAICompatibleAdapter(
        api_key="sk-LEAKED-456", base_url="https://api.example.com"
    )
    _patch_with_mock(monkeypatch, oai_compat_mod, 503, HOSTILE_BODY)
    result = await adapter.validate()
    assert result.ok is False
    assert "secret123" not in (result.error or "")
    assert "LEAKED" not in (result.error or "")
    assert "unavailable" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_openai_adapter_sanitizes_response(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-LEAKED-456")
    _patch_with_mock(monkeypatch, openai_mod, 401, HOSTILE_BODY)
    result = await adapter.validate()
    assert "LEAKED" not in (result.error or "")
    assert "secret123" not in (result.error or "")


@pytest.mark.asyncio
async def test_anthropic_adapter_sanitizes_response(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-LEAKED-456")
    _patch_with_mock(monkeypatch, anthropic_mod, 403, HOSTILE_BODY)
    result = await adapter.validate()
    assert "LEAKED" not in (result.error or "")
    assert "secret123" not in (result.error or "")


@pytest.mark.asyncio
async def test_ollama_adapter_sanitizes_response(monkeypatch):
    adapter = OllamaAdapter(
        base_url="http://ollama.example.com",
        api_key="sk-LEAKED-456",
        bearer_token="bearer-LEAK-789",
    )
    _patch_with_mock(monkeypatch, ollama_mod, 401, HOSTILE_BODY)
    result = await adapter.validate()
    assert "LEAKED" not in (result.error or "")
    assert "secret123" not in (result.error or "")
    assert "bearer-LEAK-789" not in (result.error or "")


@pytest.mark.asyncio
async def test_network_error_does_not_leak_exception_args(monkeypatch):
    """``str(exc)`` from httpx can embed the URL — assert we use type name only."""
    adapter = OpenAICompatibleAdapter(
        api_key="sk-LEAKED-456", base_url="https://internal-host.example.com"
    )

    def _exc():
        return httpx.ConnectError(
            "Connection refused (host=internal-host.example.com sk-LEAKED-456)"
        )

    _patch_with_exc(monkeypatch, oai_compat_mod, _exc)
    result = await adapter.validate()
    assert result.ok is False
    assert "LEAKED" not in (result.error or "")
    assert "internal-host" not in (result.error or "")
    # Network error contract: type name only.
    assert "ConnectError" in (result.error or "")
