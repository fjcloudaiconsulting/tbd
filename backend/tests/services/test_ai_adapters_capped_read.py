"""BE-09: every AI adapter reads provider responses under a byte cap.

An org admin chooses the ``base_url`` of ollama / openai_compatible
credentials, so a provider can answer with a huge or highly compressed
body. Every adapter request asks for ``Accept-Encoding: identity``,
refuses any other ``Content-Encoding``, stops reading once the body
passes the cap, and bounds each streamed line and the whole stream
(``app.services.ai_providers.capped_http``).
"""
from __future__ import annotations

import gzip
import json

import httpx
import pytest

from app.services.ai_providers import capped_http
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderError, ValidateResult
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter

MSGS = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]

# One 200 body every non-stream method of every adapter parses successfully.
OK_BODY = {
    "data": [{"id": "m", "embedding": [0.1]}],
    "models": [{"name": "m"}],
    "choices": [{"message": {"content": "{}"}}],
    "content": [{"type": "tool_use", "id": "t", "name": "respond_structured", "input": {}}],
    "message": {"content": "{}"},
    "embedding": [0.1],
    "usage": {},
}


def _adapters():
    return {
        "openai": OpenAIAdapter(api_key="sk-test"),
        "anthropic": AnthropicAdapter(api_key="sk-test"),
        "ollama": OllamaAdapter(base_url="https://ollama.example", api_key="x"),
        "compat": OpenAICompatibleAdapter(
            api_key="sk-test", base_url="https://llm.example/v1", base_url_is_api_root=True
        ),
    }


NON_STREAM = [
    ("openai", "validate"), ("openai", "chat"), ("openai", "embed"),
    ("openai", "chat_structured"), ("openai", "function_call"),
    ("anthropic", "validate"), ("anthropic", "chat"),
    ("anthropic", "chat_structured"), ("anthropic", "function_call"),
    ("ollama", "validate"), ("ollama", "chat"), ("ollama", "embed"),
    ("ollama", "chat_structured"), ("ollama", "function_call"),
    ("compat", "validate"), ("compat", "chat"), ("compat", "embed"),
    ("compat", "chat_structured"), ("compat", "function_call"),
]

# A valid stream per adapter: one text delta, then the end marker.
STREAM_BODIES = {
    "openai": 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n',
    "compat": 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n',
    "anthropic": 'data: {"type":"content_block_delta","delta":{"text":"Hi"}}\n\n',
    "ollama": '{"message":{"content":"Hi"}}\n{"done":true,"eval_count":1}\n',
}


async def _call(adapter, method: str):
    if method == "validate":
        return await adapter.validate()
    if method == "chat":
        return await adapter.chat(model="m", messages=MSGS)
    if method == "embed":
        return await adapter.embed(texts=["a"], model="m")
    if method == "chat_structured":
        return await adapter.chat_structured(model="m", messages=MSGS, schema=SCHEMA)
    # ``llama3.1`` passes ollama's function-calling allowlist.
    return await adapter.function_call(model="llama3.1", messages=MSGS, tools=TOOLS)


async def _outcome(adapter, method: str) -> str:
    """``ok``, the AIProviderError code, or the validate() error text."""
    try:
        result = await _call(adapter, method)
    except AIProviderError as exc:
        return exc.code
    if isinstance(result, ValidateResult) and not result.ok:
        return result.error
    return "ok"


def _expected(method: str, code: str) -> str:
    if method == "validate":
        return capped_http.validation_error(AIProviderError(code=code))
    return code


class _Chunks(httpx.AsyncByteStream):
    """A body that arrives in chunks, like a real socket (MockTransport's
    ``content=`` / ``json=`` responses are already read in full)."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.sent += 1
            yield chunk


def _install(monkeypatch, respond) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return seen


async def _drain(adapter) -> list[str]:
    return [c.delta_text async for c in adapter.stream(model="m", messages=MSGS) if not c.done]


@pytest.mark.parametrize("name,method", NON_STREAM)
async def test_baseline_body_parses(monkeypatch, name, method):
    """Control for the cases below: the same body under the cap is accepted."""
    _install(monkeypatch, lambda _r: httpx.Response(200, json=OK_BODY))
    assert await _outcome(_adapters()[name], method) == "ok"


@pytest.mark.parametrize("name,method", NON_STREAM)
async def test_request_asks_for_identity_encoding(monkeypatch, name, method):
    seen = _install(monkeypatch, lambda _r: httpx.Response(200, json=OK_BODY))
    await _outcome(_adapters()[name], method)
    assert seen and all(r.headers["accept-encoding"] == "identity" for r in seen)


@pytest.mark.parametrize("name", sorted(STREAM_BODIES))
async def test_stream_request_asks_for_identity_encoding(monkeypatch, name):
    seen = _install(
        monkeypatch, lambda _r: httpx.Response(200, content=STREAM_BODIES[name].encode())
    )
    assert await _drain(_adapters()[name]) == ["Hi"]
    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("name,method", NON_STREAM)
async def test_oversized_body_is_refused(monkeypatch, name, method):
    monkeypatch.setattr(capped_http, "MAX_BODY_BYTES", 4096)
    big = json.dumps({**OK_BODY, "pad": "x" * 8192}).encode()
    _install(monkeypatch, lambda _r: httpx.Response(200, content=big))
    assert await _outcome(_adapters()[name], method) == _expected(
        method, capped_http.RESPONSE_TOO_LARGE
    )


@pytest.mark.parametrize("name,method", NON_STREAM)
async def test_compressed_body_is_refused(monkeypatch, name, method):
    """A gzip body would be inflated past any cap; it is refused unread."""
    packed = gzip.compress(json.dumps(OK_BODY).encode())
    _install(
        monkeypatch,
        lambda _r: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=_Chunks([packed])
        ),
    )
    assert await _outcome(_adapters()[name], method) == _expected(
        method, capped_http.UNEXPECTED_ENCODING
    )


async def test_read_stops_at_the_cap(monkeypatch):
    """Default cap, chunked body: the read ends one chunk past the cap
    instead of buffering the rest."""
    chunk = b" " * 65536
    total = capped_http.MAX_BODY_BYTES // len(chunk) + 64
    body = _Chunks([b"{"] + [chunk] * total)
    _install(monkeypatch, lambda _r: httpx.Response(200, stream=body))
    result = await _adapters()["compat"].validate()
    assert result.ok is False
    assert result.error == "Provider response too large"
    assert body.sent <= capped_http.MAX_BODY_BYTES // len(chunk) + 2


async def test_oversized_error_body_keeps_the_status_and_drops_the_reason(monkeypatch):
    err = {"error": {"type": "invalid_request_error"}, "pad": "x" * capped_http.MAX_ERROR_BODY_BYTES}
    _install(monkeypatch, lambda _r: httpx.Response(400, json=err))
    with pytest.raises(AIProviderError) as ei:
        await _adapters()["compat"].chat(model="m", messages=MSGS)
    assert ei.value.code == "provider_status_400"
    assert ei.value.status_code == 400
    assert ei.value.provider_reason is None


async def test_small_error_body_still_yields_the_reason(monkeypatch):
    _install(
        monkeypatch,
        lambda _r: httpx.Response(400, json={"error": {"type": "invalid_request_error"}}),
    )
    with pytest.raises(AIProviderError) as ei:
        await _adapters()["compat"].chat(model="m", messages=MSGS)
    assert ei.value.provider_reason == "invalid_request_error"


@pytest.mark.parametrize("name", ["ollama", "compat"])
async def test_deeply_nested_json_is_invalid_json_not_a_crash(monkeypatch, name):
    _install(monkeypatch, lambda _r: httpx.Response(200, content=b"[" * 200_000))
    with pytest.raises(AIProviderError) as ei:
        await _adapters()[name].chat(model="m", messages=MSGS)
    assert ei.value.code == "provider_invalid_json"


@pytest.mark.parametrize("name", sorted(STREAM_BODIES))
async def test_stream_line_over_the_cap_is_refused(monkeypatch, name):
    monkeypatch.setattr(capped_http, "MAX_STREAM_LINE_BYTES", 1024)
    # No line break at all: an unbounded aiter_lines() would buffer it whole.
    body = _Chunks([b"data: " + b"x" * 512] * 8)
    _install(monkeypatch, lambda _r: httpx.Response(200, stream=body))
    with pytest.raises(AIProviderError) as ei:
        await _drain(_adapters()[name])
    assert ei.value.code == capped_http.RESPONSE_TOO_LARGE
    assert body.sent <= 3


@pytest.mark.parametrize("name", sorted(STREAM_BODIES))
async def test_stream_over_the_total_cap_is_refused(monkeypatch, name):
    monkeypatch.setattr(capped_http, "MAX_STREAM_BYTES", 4096)
    _install(
        monkeypatch,
        lambda _r: httpx.Response(200, stream=_Chunks([b"\n" * 1024] * 16)),
    )
    with pytest.raises(AIProviderError) as ei:
        await _drain(_adapters()[name])
    assert ei.value.code == capped_http.RESPONSE_TOO_LARGE


@pytest.mark.parametrize("name", sorted(STREAM_BODIES))
async def test_compressed_stream_is_refused(monkeypatch, name):
    packed = gzip.compress(STREAM_BODIES[name].encode())
    _install(
        monkeypatch,
        lambda _r: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=_Chunks([packed])
        ),
    )
    with pytest.raises(AIProviderError) as ei:
        await _drain(_adapters()[name])
    assert ei.value.code == capped_http.UNEXPECTED_ENCODING


@pytest.mark.parametrize("name", sorted(STREAM_BODIES))
async def test_stream_lines_split_across_chunks_are_reassembled(monkeypatch, name):
    raw = STREAM_BODIES[name].replace("\n", "\r\n").encode()
    # Three-byte chunks split lines, JSON and the CRLF pairs themselves.
    body = _Chunks([raw[i:i + 3] for i in range(0, len(raw), 3)])
    _install(monkeypatch, lambda _r: httpx.Response(200, stream=body))
    assert await _drain(_adapters()[name]) == ["Hi"]


async def test_multibyte_text_split_across_chunks_survives(monkeypatch):
    raw = 'data: {"choices":[{"delta":{"content":"€uro"}}]}\n\ndata: [DONE]\n\n'.encode()
    cut = raw.index("€".encode()) + 1
    _install(
        monkeypatch,
        lambda _r: httpx.Response(200, stream=_Chunks([raw[:cut], raw[cut:]])),
    )
    assert await _drain(_adapters()["compat"]) == ["€uro"]
