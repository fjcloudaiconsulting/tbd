"""Adapter chat() method tests (PR2 of AI tier train).

Each adapter (OpenAI, Anthropic, Ollama, OpenAI-compatible) is mocked
via ``httpx.MockTransport`` and asserted against:

- Request URL is correct for the provider.
- Auth header is present and carries the API key (or bearer token).
- Request body shape is provider-correct.
- Response is parsed into ``LLMResponse`` with the documented token
  fields.
- Errors are wrapped in ``AIProviderError`` and the wrapped message
  NEVER carries a leaked-secret marker (provider response body is
  not echoed).
"""
from __future__ import annotations

import json
from typing import Optional

import httpx
import pytest

from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import (
    OpenAICompatibleAdapter,
)


# Sentinel that, if it appears in any wrapped error message, means a
# hostile provider response body has leaked through the adapter's
# sanitization. Treat every adapter-level error catch as a test point.
LEAKED_SECRET_MARKER = "sk-LEAKED-XXX"


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Install an httpx.MockTransport on AsyncClient and capture
    requests for later assertion.
    """
    captured: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return captured


# ---------- OpenAI ----------------------------------------------------


@pytest.mark.asyncio
async def test_openai_chat_request_shape_and_response_parsing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "hello!"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    captured = _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    resp = await adapter.chat(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.content == "hello!"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.model == "gpt-4o-mini"
    req = captured[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_openai_chat_500_is_sanitized(monkeypatch):
    """A 5xx must surface a sanitized AIProviderError that does NOT
    carry the provider's response body (which could mirror a leaked
    secret).
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        # Provider body laced with a fake leaked secret.
        return httpx.Response(
            500,
            text=f"internal err — saw your key {LEAKED_SECRET_MARKER}",
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.chat(model="gpt-4o-mini", messages=[])
    msg = str(exc_info.value)
    assert LEAKED_SECRET_MARKER not in msg
    assert exc_info.value.code == "provider_status_500"


# ---------- Anthropic -------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_chat_request_shape_and_response_parsing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        # role=system is remapped to top-level system field.
        assert body["model"] == "claude-haiku-4-5"
        assert body["system"] == "You are helpful."
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        assert body["max_tokens"] == 1024
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "content": [
                    {"type": "text", "text": "pong"},
                    {"type": "text", "text": "!"},
                ],
                "usage": {"input_tokens": 11, "output_tokens": 4},
            },
        )

    captured = _install_transport(monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    resp = await adapter.chat(
        model="claude-haiku-4-5",
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "ping"},
        ],
    )
    assert resp.content == "pong!"
    assert resp.prompt_tokens == 11
    assert resp.completion_tokens == 4
    assert resp.model == "claude-haiku-4-5"
    req = captured[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test"


@pytest.mark.asyncio
async def test_anthropic_4xx_is_sanitized(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, text=f"bad key {LEAKED_SECRET_MARKER}"
        )

    _install_transport(monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.chat(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "x"}],
        )
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)
    assert exc_info.value.code == "provider_status_401"


# ---------- Ollama ---------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_chat_no_bearer(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["stream"] is False
        assert body["model"] == "llama3:8b"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "model": "llama3:8b",
                "message": {"role": "assistant", "content": "ack"},
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )

    captured = _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    resp = await adapter.chat(
        model="llama3:8b", messages=[{"role": "user", "content": "yo"}]
    )
    assert resp.content == "ack"
    assert resp.prompt_tokens == 7
    assert resp.completion_tokens == 3
    assert str(captured[0].url) == "http://10.0.0.10:11434/api/chat"


@pytest.mark.asyncio
async def test_ollama_chat_with_bearer_token(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-bearer"
        return httpx.Response(
            200,
            json={
                "model": "llama3:8b",
                "message": {"role": "assistant", "content": "ok"},
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434",
        api_key="x",
        bearer_token="secret-bearer",
    )
    resp = await adapter.chat(
        model="llama3:8b", messages=[{"role": "user", "content": "x"}]
    )
    assert resp.content == "ok"


@pytest.mark.asyncio
async def test_ollama_5xx_is_sanitized(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=f"down — {LEAKED_SECRET_MARKER}")

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.chat(
            model="llama3:8b",
            messages=[{"role": "user", "content": "x"}],
        )
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)


# ---------- OpenAI-compatible ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_chat_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "any-model"
        assert request.headers["Authorization"] == "Bearer sk-compat"
        return httpx.Response(
            200,
            json={
                "model": "any-model",
                "choices": [
                    {"message": {"role": "assistant", "content": "yo"}}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    captured = _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    resp = await adapter.chat(
        model="any-model", messages=[{"role": "user", "content": "x"}]
    )
    assert resp.content == "yo"
    assert resp.prompt_tokens == 2
    assert resp.completion_tokens == 1
    assert (
        str(captured[0].url)
        == "https://compat.example.org/v1/chat/completions"
    )


@pytest.mark.asyncio
async def test_openai_compatible_hostile_response_does_not_leak(monkeypatch):
    """Spec §10 T2-class invariant: a hostile OAI-compatible endpoint
    mirroring the request back through an error body MUST NOT leak
    the secret marker into the wrapped exception's message.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418, text=f"echo of request: {LEAKED_SECRET_MARKER}"
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.chat(
            model="any-model",
            messages=[{"role": "user", "content": "x"}],
        )
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)
    assert exc_info.value.code == "provider_status_418"
