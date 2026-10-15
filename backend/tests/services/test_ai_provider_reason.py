"""TBD-617: a provider's error type/code (never its message) rides on AIProviderError."""
from __future__ import annotations

import httpx
import pytest

from app.services.ai_providers import AIProviderError
from app.services.ai_providers import anthropic as anthropic_mod
from app.services.ai_providers import openai as openai_mod
from app.services.ai_providers import openai_compatible as compat_mod
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter

SENT = "ZQ-SENTINEL-617"
_Real = httpx.AsyncClient
MSGS = [{"role": "user", "content": "hi"}]

ADAPTERS = [
    (anthropic_mod, lambda: AnthropicAdapter(api_key="k")),
    (openai_mod, lambda: OpenAIAdapter(api_key="k")),
    (compat_mod, lambda: OpenAICompatibleAdapter(api_key="k", base_url="https://x.example")),
]


def _serve(monkeypatch, mod, status, body=None, text=None):
    def handler(_r):
        return httpx.Response(status, json=body) if text is None else httpx.Response(status, text=text)

    def client(*_a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _Real(**kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", client)


async def _fail(adapter, call):
    with pytest.raises(AIProviderError) as ei:
        if call == "chat":
            await adapter.chat(model="m", messages=MSGS, max_tokens=5)
        else:
            await adapter.function_call(model="m", messages=MSGS, tools=[], max_tokens=5)
    return ei.value


@pytest.mark.parametrize("call", ["chat", "function_call"])
@pytest.mark.parametrize("mod,make", ADAPTERS)
async def test_reason_comes_from_error_type_never_the_message(monkeypatch, mod, make, call):
    """FENCE P1. Wrong implementation: carrying ``error.message`` (or the body)."""
    _serve(monkeypatch, mod, 400, {"error": {"type": "invalid_request_error", "message": SENT}})
    exc = await _fail(make(), call)
    assert exc.provider_reason == "invalid_request_error" and exc.code == "provider_status_400"
    assert SENT not in repr(exc.__dict__) + str(exc)


@pytest.mark.parametrize("mod,make", ADAPTERS[1:])
async def test_openai_style_code_is_the_fallback(monkeypatch, mod, make):
    _serve(monkeypatch, mod, 400, {"error": {"type": "Not Valid!", "code": "context_length_exceeded"}})
    assert (await _fail(make(), "chat")).provider_reason == "context_length_exceeded"


@pytest.mark.parametrize("body,text", [
    ({"error": {"type": "has spaces " + SENT}}, None),
    ({"error": {"type": "x" * 65}}, None),
    ({"error": {"type": "UPPER"}}, None),
    ({"error": {"code": 400}}, None),
    ({"error": SENT}, None),
    ({"detail": SENT}, None),
    (None, "<html>" + SENT),
])
async def test_anything_off_pattern_is_dropped(monkeypatch, body, text):
    """FENCE P2. The type/code is provider-controlled text: only a token-shaped one passes."""
    _serve(monkeypatch, anthropic_mod, 400, body, text)
    exc = await _fail(AnthropicAdapter(api_key="k"), "chat")
    assert exc.provider_reason is None
