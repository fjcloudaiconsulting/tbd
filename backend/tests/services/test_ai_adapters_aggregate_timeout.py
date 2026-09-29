"""Aggregate-timeout fence for the 19 non-stream AI adapter methods
(TBD-329, follows the TBD-328 ``captcha.py`` precedent).

``httpx.AsyncClient(timeout=...)`` applies the bound PER PHASE (connect,
write, pool, read) — a provider that trickles bytes just under each
phase's limit keeps the call alive indefinitely, with no aggregate cap.
``httpx.MockTransport`` never triggers httpx's per-phase clocks at all,
so a handler that sleeps past the timeout constant and then returns a
valid response only fails if an ``asyncio.timeout()`` wraps the await —
that is the fence.

Each case: monkeypatch the adapter module's timeout constant to 0.05s,
install a MockTransport whose handler sleeps 1.0s then returns success,
call the method, and assert it returns/raises the adapter's timeout
outcome in well under the un-bounded 1.0s.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.services.ai_providers import anthropic as anthropic_mod
from app.services.ai_providers import ollama as ollama_mod
from app.services.ai_providers import openai as openai_mod
from app.services.ai_providers import openai_compatible as openai_compatible_mod
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderError, ValidateResult
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter


SCHEMA = {
    "type": "object",
    "required": ["category"],
    "properties": {"category": {"type": "string"}},
}
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
DEADLINE = 0.5  # generous ceiling well under the 1.0s handler sleep


def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)


async def _slow_handler(sleep_s: float, response_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        if sleep_s:
            await asyncio.sleep(sleep_s)
        return response_factory(request)

    return handler


def _openai_chat_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def _openai_models_ok(_request):
    return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})


def _openai_embed_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "text-embedding-3-small",
            "data": [{"embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1},
        },
    )


def _anthropic_chat_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _anthropic_models_ok(_request):
    return httpx.Response(200, json={"data": [{"id": "claude-haiku-4-5"}]})


def _anthropic_structured_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "claude-haiku-4-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "respond_structured",
                    "input": {"category": "rent"},
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _anthropic_function_call_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "claude-haiku-4-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "set_category",
                    "input": {"slug": "rent"},
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _ollama_tags_ok(_request):
    return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})


def _ollama_chat_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "llama3:8b",
            "message": {"content": "hi"},
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
    )


def _ollama_embed_ok(_request):
    return httpx.Response(
        200, json={"model": "nomic-embed-text", "embedding": [0.1, 0.2]}
    )


def _ollama_function_call_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "llama3.1:8b",
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "set_category", "arguments": {"slug": "food"}}}
                ],
            },
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
    )


def _openai_compat_models_ok(_request):
    return httpx.Response(200, json={"data": [{"id": "any-model"}]})


def _openai_compat_chat_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "any-model",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def _openai_compat_embed_ok(_request):
    return httpx.Response(
        200,
        json={
            "model": "my-embed-model",
            "data": [{"embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1},
        },
    )


# Each case: (id, module, timeout_attr, setup, call, assert_outcome)
# ``setup`` returns the adapter; ``call`` is an async fn(adapter) that
# performs the network call and must raise/return the timeout outcome.


async def _run_timed(monkeypatch, module, timeout_attr, response_factory, call):
    monkeypatch.setattr(module, timeout_attr, 0.05)
    handler = await _slow_handler(1.0, response_factory)
    _install_transport(monkeypatch, handler)
    start = time.monotonic()
    result = await call()
    elapsed = time.monotonic() - start
    assert elapsed < DEADLINE, f"call took {elapsed}s — aggregate bound not enforced"
    return result


async def _run_control(monkeypatch, module, timeout_attr, response_factory, call):
    monkeypatch.setattr(module, timeout_attr, 5.0)
    handler = await _slow_handler(0.0, response_factory)
    _install_transport(monkeypatch, handler)
    return await call()


# ---------- Anthropic (4 sites) ---------------------------------------


@pytest.mark.asyncio
async def test_anthropic_validate_aggregate_timeout(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        return await adapter.validate()

    result = await _run_timed(
        monkeypatch, anthropic_mod, "VALIDATE_TIMEOUT_S", _anthropic_models_ok, call
    )
    assert isinstance(result, ValidateResult)
    assert result.ok is False
    assert result.error == "Network error: TimeoutError"


@pytest.mark.asyncio
async def test_anthropic_validate_control(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        return await adapter.validate()

    result = await _run_control(
        monkeypatch, anthropic_mod, "VALIDATE_TIMEOUT_S", _anthropic_models_ok, call
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_anthropic_chat_aggregate_timeout(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        await adapter.chat(model="claude-haiku-4-5", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, anthropic_mod, "CHAT_TIMEOUT_S", _anthropic_chat_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_anthropic_chat_control(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        return await adapter.chat(
            model="claude-haiku-4-5", messages=[{"role": "user", "content": "hi"}]
        )

    result = await _run_control(
        monkeypatch, anthropic_mod, "CHAT_TIMEOUT_S", _anthropic_chat_ok, call
    )
    assert result.content == "hi"


@pytest.mark.asyncio
async def test_anthropic_chat_structured_aggregate_timeout(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        await adapter.chat_structured(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, anthropic_mod, "CHAT_TIMEOUT_S", _anthropic_structured_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_anthropic_chat_structured_control(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        return await adapter.chat_structured(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    result = await _run_control(
        monkeypatch, anthropic_mod, "CHAT_TIMEOUT_S", _anthropic_structured_ok, call
    )
    assert result.content


@pytest.mark.asyncio
async def test_anthropic_function_call_aggregate_timeout(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        await adapter.function_call(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, anthropic_mod, "CHAT_TIMEOUT_S", _anthropic_function_call_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_anthropic_function_call_control(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    async def call():
        return await adapter.function_call(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    result = await _run_control(
        monkeypatch, anthropic_mod, "CHAT_TIMEOUT_S", _anthropic_function_call_ok, call
    )
    assert result.tool_calls


# ---------- OpenAI (5 sites) -------------------------------------------


@pytest.mark.asyncio
async def test_openai_validate_aggregate_timeout(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        return await adapter.validate()

    result = await _run_timed(
        monkeypatch, openai_mod, "VALIDATE_TIMEOUT_S", _openai_models_ok, call
    )
    assert result.ok is False
    assert result.error == "Network error: TimeoutError"


@pytest.mark.asyncio
async def test_openai_validate_control(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        return await adapter.validate()

    result = await _run_control(
        monkeypatch, openai_mod, "VALIDATE_TIMEOUT_S", _openai_models_ok, call
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_openai_chat_aggregate_timeout(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        await adapter.chat(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, openai_mod, "CHAT_TIMEOUT_S", _openai_chat_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_chat_control(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        return await adapter.chat(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )

    result = await _run_control(monkeypatch, openai_mod, "CHAT_TIMEOUT_S", _openai_chat_ok, call)
    assert result.content == "hi"


@pytest.mark.asyncio
async def test_openai_embed_aggregate_timeout(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        await adapter.embed(texts=["hello"])

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, openai_mod, "EMBED_TIMEOUT_S", _openai_embed_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_embed_control(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        return await adapter.embed(texts=["hello"])

    result = await _run_control(monkeypatch, openai_mod, "EMBED_TIMEOUT_S", _openai_embed_ok, call)
    assert result.vectors


@pytest.mark.asyncio
async def test_openai_chat_structured_aggregate_timeout(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        await adapter.chat_structured(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, openai_mod, "CHAT_TIMEOUT_S", _openai_chat_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_chat_structured_control(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        return await adapter.chat_structured(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    result = await _run_control(monkeypatch, openai_mod, "CHAT_TIMEOUT_S", _openai_chat_ok, call)
    assert result.content


@pytest.mark.asyncio
async def test_openai_function_call_aggregate_timeout(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        await adapter.function_call(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, openai_mod, "CHAT_TIMEOUT_S", _openai_chat_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_function_call_control(monkeypatch):
    adapter = OpenAIAdapter(api_key="sk-test")

    async def call():
        return await adapter.function_call(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    result = await _run_control(monkeypatch, openai_mod, "CHAT_TIMEOUT_S", _openai_chat_ok, call)
    assert result.content is not None


# ---------- OpenAI-compatible (5 sites) --------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_validate_aggregate_timeout(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        return await adapter.validate()

    result = await _run_timed(
        monkeypatch,
        openai_compatible_mod,
        "VALIDATE_TIMEOUT_S",
        _openai_compat_models_ok,
        call,
    )
    assert result.ok is False
    assert result.error == "Network error: TimeoutError"


@pytest.mark.asyncio
async def test_openai_compatible_validate_control(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        return await adapter.validate()

    result = await _run_control(
        monkeypatch,
        openai_compatible_mod,
        "VALIDATE_TIMEOUT_S",
        _openai_compat_models_ok,
        call,
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_openai_compatible_chat_aggregate_timeout(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        await adapter.chat(model="any-model", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, openai_compatible_mod, "CHAT_TIMEOUT_S", _openai_compat_chat_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_compatible_chat_control(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        return await adapter.chat(
            model="any-model", messages=[{"role": "user", "content": "hi"}]
        )

    result = await _run_control(
        monkeypatch, openai_compatible_mod, "CHAT_TIMEOUT_S", _openai_compat_chat_ok, call
    )
    assert result.content == "hi"


@pytest.mark.asyncio
async def test_openai_compatible_embed_aggregate_timeout(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        await adapter.embed(texts=["x"], model="my-embed-model")

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, openai_compatible_mod, "EMBED_TIMEOUT_S", _openai_compat_embed_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_compatible_embed_control(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        return await adapter.embed(texts=["x"], model="my-embed-model")

    result = await _run_control(
        monkeypatch, openai_compatible_mod, "EMBED_TIMEOUT_S", _openai_compat_embed_ok, call
    )
    assert result.vectors


@pytest.mark.asyncio
async def test_openai_compatible_chat_structured_aggregate_timeout(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        await adapter.chat_structured(
            model="any-model",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, openai_compatible_mod, "CHAT_TIMEOUT_S", _openai_compat_chat_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_compatible_chat_structured_control(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        return await adapter.chat_structured(
            model="any-model",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    result = await _run_control(
        monkeypatch, openai_compatible_mod, "CHAT_TIMEOUT_S", _openai_compat_chat_ok, call
    )
    assert result.content


@pytest.mark.asyncio
async def test_openai_compatible_function_call_aggregate_timeout(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        await adapter.function_call(
            model="any-model",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, openai_compatible_mod, "CHAT_TIMEOUT_S", _openai_compat_chat_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_openai_compatible_function_call_control(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-compat", base_url="https://compat.example.org")

    async def call():
        return await adapter.function_call(
            model="any-model",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    result = await _run_control(
        monkeypatch, openai_compatible_mod, "CHAT_TIMEOUT_S", _openai_compat_chat_ok, call
    )
    assert result.content is not None


# ---------- Ollama (5 sites) -------------------------------------------


@pytest.mark.asyncio
async def test_ollama_validate_aggregate_timeout(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        return await adapter.validate()

    result = await _run_timed(
        monkeypatch, ollama_mod, "VALIDATE_TIMEOUT_S", _ollama_tags_ok, call
    )
    assert result.ok is False
    assert result.error == "Network error: TimeoutError"


@pytest.mark.asyncio
async def test_ollama_validate_control(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        return await adapter.validate()

    result = await _run_control(
        monkeypatch, ollama_mod, "VALIDATE_TIMEOUT_S", _ollama_tags_ok, call
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_ollama_chat_aggregate_timeout(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        await adapter.chat(model="llama3:8b", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, ollama_mod, "CHAT_TIMEOUT_S", _ollama_chat_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_ollama_chat_control(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        return await adapter.chat(
            model="llama3:8b", messages=[{"role": "user", "content": "hi"}]
        )

    result = await _run_control(monkeypatch, ollama_mod, "CHAT_TIMEOUT_S", _ollama_chat_ok, call)
    assert result.content == "hi"


@pytest.mark.asyncio
async def test_ollama_embed_aggregate_timeout(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        await adapter.embed(texts=["a"], model="nomic-embed-text")

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, ollama_mod, "EMBED_TIMEOUT_S", _ollama_embed_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_ollama_embed_control(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        return await adapter.embed(texts=["a"], model="nomic-embed-text")

    result = await _run_control(monkeypatch, ollama_mod, "EMBED_TIMEOUT_S", _ollama_embed_ok, call)
    assert result.vectors


@pytest.mark.asyncio
async def test_ollama_chat_structured_aggregate_timeout(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        await adapter.chat_structured(
            model="llama3:8b",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(monkeypatch, ollama_mod, "CHAT_TIMEOUT_S", _ollama_chat_ok, call)
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_ollama_chat_structured_control(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        return await adapter.chat_structured(
            model="llama3:8b",
            messages=[{"role": "user", "content": "hi"}],
            schema=SCHEMA,
        )

    result = await _run_control(monkeypatch, ollama_mod, "CHAT_TIMEOUT_S", _ollama_chat_ok, call)
    assert result.content


@pytest.mark.asyncio
async def test_ollama_function_call_aggregate_timeout(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        await adapter.function_call(
            model="llama3.1:8b",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    with pytest.raises(AIProviderError) as exc_info:
        await _run_timed(
            monkeypatch, ollama_mod, "CHAT_TIMEOUT_S", _ollama_function_call_ok, call
        )
    assert exc_info.value.code == "network_TimeoutError"


@pytest.mark.asyncio
async def test_ollama_function_call_control(monkeypatch):
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")

    async def call():
        return await adapter.function_call(
            model="llama3.1:8b",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS_OPENAI,
        )

    result = await _run_control(
        monkeypatch, ollama_mod, "CHAT_TIMEOUT_S", _ollama_function_call_ok, call
    )
    assert result.tool_calls


@pytest.mark.asyncio
async def test_ollama_embed_deadline_spans_the_whole_batch(monkeypatch):
    """ollama embeds one text per POST, so the bound must be ONE deadline
    across the batch. Three texts at 0.04s each fit a 0.05s per-request
    bound (0.12s total) but not a 0.05s batch deadline."""
    adapter = OllamaAdapter(base_url="http://10.0.0.10:11434", api_key="x")
    monkeypatch.setattr(ollama_mod, "EMBED_TIMEOUT_S", 0.05)
    _install_transport(monkeypatch, await _slow_handler(0.04, _ollama_embed_ok))
    start = time.monotonic()
    with pytest.raises(AIProviderError) as exc_info:
        await adapter.embed(texts=["a", "b", "c"], model="nomic-embed-text")
    assert exc_info.value.code == "network_TimeoutError"
    assert time.monotonic() - start < 0.1
