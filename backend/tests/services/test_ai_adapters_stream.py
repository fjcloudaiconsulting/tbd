"""Adapter ``stream()`` tests (PR3 of AI tier train).

Pins:
- OpenAI: SSE chunks ``data: {...}\\n\\n``; final ``data: [DONE]`` ends
  the loop. ``stream_options.include_usage`` makes the provider emit a
  final usage block that we capture into ``StreamChunk.final_usage``.
- Anthropic: SSE with ``content_block_delta`` events. Token usage
  comes from ``message_start`` (prompt) and ``message_delta`` (output).
- Ollama: NDJSON (one JSON per line), final line has ``done: true``
  with the eval counts.
- OpenAI-compatible: same shape as OpenAI.
- The ledger row written by ``call_llm_stream`` is exercised in
  ``test_ai_dispatch_capabilities.py``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import (
    OpenAICompatibleAdapter,
)


def _install_streaming_transport(monkeypatch, response: httpx.Response):
    """Install a transport that returns ``response`` from any request."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)


# ---------- OpenAI ---------------------------------------------------


@pytest.mark.asyncio
async def test_openai_stream_yields_chunks_and_captures_final_usage(monkeypatch):
    sse_body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )
    _install_streaming_transport(
        monkeypatch,
        httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        ),
    )
    adapter = OpenAIAdapter(api_key="sk-test")
    chunks = []
    async for c in adapter.stream(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(c)
    # 2 deltas + 1 done.
    assert [c.delta_text for c in chunks if not c.done] == ["Hel", "lo"]
    final = chunks[-1]
    assert final.done is True
    assert final.final_usage is not None
    assert final.final_usage.prompt_tokens == 3
    assert final.final_usage.completion_tokens == 2


# ---------- Anthropic ------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_stream_yields_chunks_and_captures_usage(monkeypatch):
    sse_body = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
        'data: {"type":"content_block_delta","delta":{"text":"He"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"text":"llo"}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":3}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    _install_streaming_transport(
        monkeypatch,
        httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        ),
    )
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    chunks = []
    async for c in adapter.stream(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(c)
    assert [c.delta_text for c in chunks if not c.done] == ["He", "llo"]
    final = chunks[-1]
    assert final.done is True
    assert final.final_usage.prompt_tokens == 4
    assert final.final_usage.completion_tokens == 3


# ---------- Ollama ---------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_stream_yields_chunks_and_captures_eval_counts(monkeypatch):
    ndjson_body = (
        json.dumps({"message": {"content": "Hi "}, "done": False})
        + "\n"
        + json.dumps({"message": {"content": "there"}, "done": False})
        + "\n"
        + json.dumps(
            {
                "message": {"content": ""},
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 4,
            }
        )
        + "\n"
    )
    _install_streaming_transport(
        monkeypatch,
        httpx.Response(
            200,
            content=ndjson_body.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        ),
    )
    adapter = OllamaAdapter(
        base_url="http://10.0.0.10:11434", api_key="x"
    )
    chunks = []
    async for c in adapter.stream(
        model="llama3:8b",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)
    assert [c.delta_text for c in chunks if not c.done] == ["Hi ", "there"]
    final = chunks[-1]
    assert final.done is True
    assert final.final_usage.prompt_tokens == 5
    assert final.final_usage.completion_tokens == 4


# ---------- OpenAI-compatible ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_stream_yields_chunks(monkeypatch):
    sse_body = (
        'data: {"choices":[{"delta":{"content":"yo"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    _install_streaming_transport(
        monkeypatch,
        httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        ),
    )
    adapter = OpenAICompatibleAdapter(
        api_key="sk-compat", base_url="https://compat.example.org"
    )
    chunks = []
    async for c in adapter.stream(
        model="any", messages=[{"role": "user", "content": "x"}]
    ):
        chunks.append(c)
    assert [c.delta_text for c in chunks if not c.done] == ["yo"]
    assert chunks[-1].done is True
