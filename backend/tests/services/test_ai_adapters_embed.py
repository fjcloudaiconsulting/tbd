"""Adapter ``embed()`` tests (PR3 of AI tier train).

OpenAI, Ollama, OpenAI-compatible: happy path + sanitized error.
Anthropic: NotImplementedError (no public embeddings API).

Sanitization invariant from PR2 carries through — provider error
bodies must NEVER leak into the wrapped exception's message.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import (
    OpenAICompatibleAdapter,
)


LEAKED_SECRET_MARKER = "sk-LEAKED-XXX"


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
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


# ---------- OpenAI ---------------------------------------------------


@pytest.mark.asyncio
async def test_openai_embed_happy_path_default_model(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == ["hello", "world"]
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-3-small",
                "data": [
                    {"embedding": [0.1, 0.2, 0.3]},
                    {"embedding": [0.4, 0.5, 0.6]},
                ],
                "usage": {"prompt_tokens": 4},
            },
        )

    captured = _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    resp = await adapter.embed(texts=["hello", "world"])
    assert resp.vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert resp.model == "text-embedding-3-small"
    assert resp.prompt_tokens == 4
    assert str(captured[0].url) == "https://api.openai.com/v1/embeddings"
    assert captured[0].headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_openai_embed_explicit_model_overrides_default(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "text-embedding-3-large"
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-3-large",
                "data": [{"embedding": [0.1]}],
                "usage": {"prompt_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    resp = await adapter.embed(texts=["x"], model="text-embedding-3-large")
    assert resp.model == "text-embedding-3-large"


@pytest.mark.asyncio
async def test_openai_embed_error_sanitized(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, text=f"crashed, key was {LEAKED_SECRET_MARKER}"
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.embed(texts=["x"])
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)
    assert exc_info.value.code == "provider_status_500"


# ---------- Anthropic ------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_embed_raises_not_implemented():
    """Anthropic does not expose a public embeddings API.

    Documented refusal — caller (service or feature surface) can pick
    a sibling provider that has embeddings configured.
    """
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    with pytest.raises(NotImplementedError) as exc_info:
        await adapter.embed(texts=["x"])
    assert "Anthropic" in str(exc_info.value)


# ---------- Ollama ---------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_embed_no_bearer(monkeypatch):
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests_seen.append(body)
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "model": "nomic-embed-text",
                "embedding": [0.7, 0.8, 0.9],
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    resp = await adapter.embed(texts=["a", "b"], model="nomic-embed-text")
    # One request per text.
    assert len(requests_seen) == 2
    assert resp.vectors == [[0.7, 0.8, 0.9], [0.7, 0.8, 0.9]]
    assert resp.model == "nomic-embed-text"
    # Estimated tokens (4 chars / token), minimum 1.
    assert resp.prompt_tokens >= 1


@pytest.mark.asyncio
async def test_ollama_embed_with_bearer_token(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-bearer"
        return httpx.Response(
            200,
            json={"model": "nomic-embed-text", "embedding": [0.1, 0.2]},
        )

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434",
        api_key="x",
        bearer_token="secret-bearer",
    )
    resp = await adapter.embed(texts=["x"], model="nomic-embed-text")
    assert resp.vectors == [[0.1, 0.2]]


@pytest.mark.asyncio
async def test_ollama_embed_error_sanitized(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, text=f"down — {LEAKED_SECRET_MARKER}"
        )

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.embed(texts=["x"], model="nomic-embed-text")
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)


# ---------- OpenAI-compatible ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_embed_happy(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "my-embed-model"
        assert request.headers["Authorization"] == "Bearer sk-compat"
        return httpx.Response(
            200,
            json={
                "model": "my-embed-model",
                "data": [{"embedding": [1.0, 2.0]}],
                "usage": {"prompt_tokens": 7},
            },
        )

    captured = _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    resp = await adapter.embed(texts=["x"], model="my-embed-model")
    assert resp.vectors == [[1.0, 2.0]]
    assert resp.prompt_tokens == 7
    assert (
        str(captured[0].url) == "https://compat.example.org/v1/embeddings"
    )


@pytest.mark.asyncio
async def test_openai_compatible_embed_hostile_body_does_not_leak(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418, text=f"echo of request: {LEAKED_SECRET_MARKER}"
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.embed(texts=["x"], model="my-embed-model")
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)
