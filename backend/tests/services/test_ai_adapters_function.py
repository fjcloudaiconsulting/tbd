"""Adapter ``function_call()`` tests (PR3 of AI tier train).

Wire-shape pins per provider:

- OpenAI: tools array passed through; ``tool_calls[*].function.arguments``
  is a JSON string that the adapter parses into a dict.
- Anthropic: OpenAI-shape tools normalized to ``{name, description,
  input_schema}``; response ``content`` blocks of type ``tool_use``
  parsed into ``FunctionCallResponse.tool_calls``.
- Ollama: refuses with ``CapabilityNotSupported`` for models not on
  the known function-call list; passes through for known prefixes.
- OpenAI-compatible: same shape as OpenAI; provider-side rejection
  surfaces as a sanitized ``AIProviderError``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import (
    AIProviderError,
    CapabilityNotSupported,
)
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import (
    OpenAICompatibleAdapter,
)


TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "set_category",
            "description": "Assign a category to the transaction.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    }
]


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
async def test_openai_function_call_parses_tool_calls(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == TOOLS_OPENAI
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "set_category",
                                        "arguments": '{"slug": "rent"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="sk-test")
    resp = await adapter.function_call(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "classify"}],
        tools=TOOLS_OPENAI,
    )
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["name"] == "set_category"
    assert resp.tool_calls[0]["arguments"] == {"slug": "rent"}
    assert resp.prompt_tokens == 9
    assert resp.completion_tokens == 4


# ---------- Anthropic ------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_function_call_normalizes_oai_tools_and_parses_tool_use(
    monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        # Tools were normalized from OAI shape into Anthropic shape.
        assert len(body["tools"]) == 1
        assert body["tools"][0]["name"] == "set_category"
        assert "input_schema" in body["tools"][0]
        assert "type" not in body["tools"][0]  # no "function" wrapper
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "content": [
                    {"type": "text", "text": "Looks like rent."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "set_category",
                        "input": {"slug": "rent"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    resp = await adapter.function_call(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "classify"}],
        tools=TOOLS_OPENAI,
    )
    assert resp.tool_calls == [
        {"name": "set_category", "arguments": {"slug": "rent"}}
    ]
    assert resp.content == "Looks like rent."


# ---------- Ollama ---------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_function_call_refuses_unsupported_model():
    """An Ollama model not on the known function-call prefix list must
    raise ``CapabilityNotSupported`` — the caller (call_llm_function)
    surfaces that as 412 ``ai_capability_not_supported``.
    """
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    with pytest.raises(CapabilityNotSupported) as exc_info:
        await adapter.function_call(
            model="phi:latest",  # not on the list
            messages=[],
            tools=TOOLS_OPENAI,
        )
    assert exc_info.value.model == "phi:latest"
    assert exc_info.value.capability == "function_call"


@pytest.mark.asyncio
async def test_ollama_function_call_supported_model_passes_through(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == TOOLS_OPENAI
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "set_category",
                                "arguments": {"slug": "food"},
                            }
                        }
                    ],
                },
                "prompt_eval_count": 6,
                "eval_count": 2,
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    resp = await adapter.function_call(
        model="llama3.1:8b",
        messages=[{"role": "user", "content": "classify"}],
        tools=TOOLS_OPENAI,
    )
    assert resp.tool_calls == [
        {"name": "set_category", "arguments": {"slug": "food"}}
    ]


# ---------- OpenAI-compatible ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_function_call_passes_through(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == TOOLS_OPENAI
        return httpx.Response(
            200,
            json={
                "model": "any",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "set_category",
                                        "arguments": '{"slug": "utilities"}',
                                    }
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
        )

    _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    resp = await adapter.function_call(
        model="any",
        messages=[{"role": "user", "content": "x"}],
        tools=TOOLS_OPENAI,
    )
    assert resp.tool_calls == [
        {"name": "set_category", "arguments": {"slug": "utilities"}}
    ]


@pytest.mark.asyncio
async def test_openai_compatible_function_call_provider_400_bubbles(monkeypatch):
    """An OAI-compatible server that doesn't support tool calls
    typically returns 400. The adapter must surface that as a
    sanitized ``AIProviderError`` so the caller can fall back.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="tools unsupported by this model")

    _install_transport(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.function_call(
            model="any", messages=[], tools=TOOLS_OPENAI
        )
    assert exc_info.value.code == "provider_status_400"
