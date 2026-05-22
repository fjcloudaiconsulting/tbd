"""Adapter ``chat_structured()`` tests (PR3 of AI tier train).

These pin the WIRE shape each adapter sends to the provider, NOT the
retry-cap budget — that lives in the service layer
(``call_llm_structured`` in ``test_ai_dispatch``).

Wire shape per provider:

- OpenAI gpt-4o-mini: ``response_format = {"type": "json_schema",
  "json_schema": {...}}``.
- OpenAI legacy / non-4o: ``response_format = {"type": "json_object"}``
  + schema in the system message.
- Anthropic: single ``tools`` entry whose ``input_schema`` is the
  response schema; ``tool_choice`` pins the tool.
- Ollama: ``format: "json"`` + schema-hint system message.
- OpenAI-compatible: same as legacy OpenAI (json_object only).
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


SCHEMA = {
    "type": "object",
    "required": ["category"],
    "properties": {"category": {"type": "string"}},
}
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
async def test_openai_chat_structured_uses_json_schema_for_gpt4o(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"category": "groceries"}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    resp = await adapter.chat_structured(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "classify"}],
        schema=SCHEMA,
    )
    assert resp.content == '{"category": "groceries"}'
    assert resp.prompt_tokens == 5
    assert resp.completion_tokens == 3


@pytest.mark.asyncio
async def test_openai_chat_structured_falls_back_to_json_object_for_legacy(
    monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["response_format"] == {"type": "json_object"}
        # Schema hint prepended as system message.
        first = body["messages"][0]
        assert first["role"] == "system"
        assert "category" in first["content"]
        return httpx.Response(
            200,
            json={
                "model": "gpt-3.5-turbo",
                "choices": [
                    {
                        "message": {
                            "content": '{"category": "transport"}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    resp = await adapter.chat_structured(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "classify"}],
        schema=SCHEMA,
    )
    assert resp.content == '{"category": "transport"}'


@pytest.mark.asyncio
async def test_openai_chat_structured_error_sanitized(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"err {LEAKED_SECRET_MARKER}")

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.chat_structured(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "x"}],
            schema=SCHEMA,
        )
    assert LEAKED_SECRET_MARKER not in str(exc_info.value)


# ---------- Anthropic ------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_chat_structured_uses_tool_use(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert len(body["tools"]) == 1
        assert body["tools"][0]["name"] == "respond_structured"
        assert body["tools"][0]["input_schema"] == SCHEMA
        assert body["tool_choice"]["name"] == "respond_structured"
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "respond_structured",
                        "input": {"category": "utilities"},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 6},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    resp = await adapter.chat_structured(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "classify"}],
        schema=SCHEMA,
    )
    # Content is the JSON serialization of the tool_use.input dict.
    parsed = json.loads(resp.content)
    assert parsed == {"category": "utilities"}
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 6


# ---------- Ollama ---------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_chat_structured_uses_format_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["format"] == "json"
        # Schema hint sits in the prepended system message.
        first = body["messages"][0]
        assert first["role"] == "system"
        assert "category" in first["content"]
        return httpx.Response(
            200,
            json={
                "model": "llama3:8b",
                "message": {
                    "role": "assistant",
                    "content": '{"category": "food"}',
                },
                "prompt_eval_count": 8,
                "eval_count": 4,
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    resp = await adapter.chat_structured(
        model="llama3:8b",
        messages=[{"role": "user", "content": "classify"}],
        schema=SCHEMA,
    )
    assert json.loads(resp.content) == {"category": "food"}
    assert resp.prompt_tokens == 8


# ---------- OpenAI-compatible ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_chat_structured_uses_json_object(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "model": "any",
                "choices": [
                    {"message": {"content": '{"category": "rent"}'}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    resp = await adapter.chat_structured(
        model="any",
        messages=[{"role": "user", "content": "x"}],
        schema=SCHEMA,
    )
    assert json.loads(resp.content) == {"category": "rent"}
