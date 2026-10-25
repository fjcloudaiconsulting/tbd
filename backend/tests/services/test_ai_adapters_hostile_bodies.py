"""Hostile provider bodies on every adapter capability (TBD-607).

``function_call`` was hardened in the multi-round work; these tables pin
the same contract on ``chat``, ``chat_structured``, ``stream``, ``embed``
and ``validate``: a malformed, wrongly shaped or out-of-range body ends
in the typed ``AIProviderError`` (or a failed ``ValidateResult``), never
an untyped exception, and a reported token count is never negative.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderError, token_count
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter

USER = {"role": "user", "content": "hi"}
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
DEEP = b"[" * 100_000 + b"]" * 100_000


def _serve(monkeypatch, raw: bytes, content_type: str = "application/json") -> None:
    response = lambda _req: httpx.Response(  # noqa: E731
        200, content=raw, headers={"Content-Type": content_type}
    )
    transport = httpx.MockTransport(response)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)


def _openai():
    return OpenAIAdapter(api_key="sk-test-fixture")


def _compat():
    return OpenAICompatibleAdapter(api_key="sk-test-fixture", base_url="https://c.example.org")


def _anthropic():
    return AnthropicAdapter(api_key="sk-test-fixture")


OPENAI_USAGE = ("prompt_tokens", "completion_tokens")
ANTHROPIC_USAGE = ("input_tokens", "output_tokens")


def _openai_body(usage=None, **over):
    body = {
        "choices": [{"message": {"content": "x"}}],
        "usage": usage if usage is not None else {"prompt_tokens": 1, "completion_tokens": 1},
        "model": "m",
    }
    body.update(over)
    return body


def _anthropic_body(usage=None, **over):
    body = {
        "content": [
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": "toolu_1", "name": "respond_structured", "input": {"a": 1}},
        ],
        "usage": usage if usage is not None else {"input_tokens": 1, "output_tokens": 1},
        "model": "m",
    }
    body.update(over)
    return body


# Usage values a hostile endpoint can report, each rejected as a whole response.
BAD_COUNTS = {
    "non-numeric": "abc",
    "numeric-string": "12",
    "infinite": float("inf"),
    "nan": float("nan"),
    "fractional": 1.5,
    "boolean": True,
    "huge": 10**12,
    "object": {"n": 1},
}

# Payload-level shapes every non-stream path must reject.
COMMON_RAW = {
    "deep-nesting": DEEP,
    "non-json": b"<html>not json</html>",
    "empty": b"",
    "list-payload": b"[1, 2]",
    "string-payload": b'"x"',
    "null-payload": b"null",
    "missing-keys": b"{}",
}


def _raw_cases(body_fn, usage_keys, shape_cases):
    cases = dict(COMMON_RAW)
    for name, value in BAD_COUNTS.items():
        for key in usage_keys:
            usage = {k: 1 for k in usage_keys}
            usage[key] = value
            cases[f"{key}-{name}"] = json.dumps(body_fn(usage=usage)).encode()
    cases["usage-list"] = json.dumps(body_fn(usage=[1])).encode()
    cases["usage-string"] = json.dumps(body_fn(usage="lots")).encode()
    for name, body in shape_cases.items():
        cases[name] = json.dumps(body).encode()
    return cases


OPENAI_CASES = _raw_cases(
    _openai_body,
    OPENAI_USAGE,
    {
        "choices-int": _openai_body(choices=5),
        "choices-empty": _openai_body(choices=[]),
        "choice-int": _openai_body(choices=[5]),
        "message-list": _openai_body(choices=[{"message": [1]}]),
        "content-object": _openai_body(choices=[{"message": {"content": {"a": 1}}}]),
        "content-list": _openai_body(choices=[{"message": {"content": [1]}}]),
    },
)

ANTHROPIC_CASES = _raw_cases(
    _anthropic_body,
    ANTHROPIC_USAGE,
    {
        "blocks-int": _anthropic_body(content=5),
        "blocks-object": _anthropic_body(content={"type": "text"}),
        "blocks-missing": {"usage": {"input_tokens": 1, "output_tokens": 1}},
    },
)


# Free text next to tool calls is optional, so ``function_call`` turns a
# non-string ``content`` into "" (test_ai_adapters_multi_round.py) instead.
FUNCTION_CALL_TOLERATED = {"content-object", "content-list"}


def _non_stream_matrix():
    calls = {
        "chat": lambda a: a.chat(model="m", messages=[USER]),
        "chat_structured": lambda a: a.chat_structured(model="m", messages=[USER], schema=SCHEMA),
        "function_call": lambda a: a.function_call(model="m", messages=[USER], tools=TOOLS),
    }
    rows = []
    for adapter_name, factory, cases in (
        ("openai", _openai, OPENAI_CASES),
        ("openai_compatible", _compat, OPENAI_CASES),
        ("anthropic", _anthropic, ANTHROPIC_CASES),
    ):
        for path, call in calls.items():
            for case, raw in cases.items():
                if path == "function_call" and case in FUNCTION_CALL_TOLERATED:
                    continue
                rows.append(
                    pytest.param(factory, call, raw, id=f"{adapter_name}-{path}-{case}")
                )
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("factory,call,raw", _non_stream_matrix())
async def test_hostile_body_is_a_typed_provider_error(monkeypatch, factory, call, raw):
    _serve(monkeypatch, raw)
    with pytest.raises(AIProviderError) as exc:
        await call(factory())
    assert exc.value.code in {"provider_invalid_json", "provider_unexpected_shape"}


# ---------- embed ----------------------------------------------------------


def _embed_body(usage=None, **over):
    body = {
        "data": [{"embedding": [0.1, 0.2]}],
        "usage": usage if usage is not None else {"prompt_tokens": 1},
        "model": "m",
    }
    body.update(over)
    return body


EMBED_CASES = _raw_cases(
    _embed_body,
    ("prompt_tokens",),
    {
        "data-int": _embed_body(data=5),
        "embedding-object": _embed_body(data=[{"embedding": {"a": 1}}]),
        "embedding-string-values": _embed_body(data=[{"embedding": ["x"]}]),
        "embedding-infinite": _embed_body(data=[{"embedding": [float("inf")]}]),
    },
)
# A missing ``data`` list is an empty embedding set, not a shape error.
EMBED_CASES.pop("missing-keys")


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_openai, _compat], ids=["openai", "openai_compatible"])
@pytest.mark.parametrize("raw", list(EMBED_CASES.values()), ids=list(EMBED_CASES))
async def test_hostile_embed_body_is_a_typed_provider_error(monkeypatch, factory, raw):
    _serve(monkeypatch, raw)
    with pytest.raises(AIProviderError):
        await factory().embed(texts=["a"], model="m")


# ---------- validate --------------------------------------------------------

VALIDATE_CASES = {
    "deep-nesting": DEEP,
    "non-json": b"<html>",
    "list-payload": b"[1]",
    "null-payload": b"null",
    "data-int": b'{"data": 5}',
    "data-string": b'{"data": "abc"}',
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory", [_openai, _compat, _anthropic], ids=["openai", "openai_compatible", "anthropic"]
)
@pytest.mark.parametrize("raw", list(VALIDATE_CASES.values()), ids=list(VALIDATE_CASES))
async def test_hostile_models_body_fails_validation_cleanly(monkeypatch, factory, raw):
    _serve(monkeypatch, raw)
    result = await factory().validate()
    assert result.ok is False
    assert result.error


# ---------- stream ----------------------------------------------------------


def _sse(*events) -> bytes:
    lines = []
    for e in events:
        data = e if isinstance(e, (str, bytes)) else json.dumps(e)
        if isinstance(data, bytes):
            data = data.decode()
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode()


OPENAI_DELTA = {"choices": [{"delta": {"content": "x"}}]}


def _openai_usage_event(usage):
    return {"choices": [], "usage": usage}


OPENAI_STREAM_CASES = {
    "event-list": _sse(OPENAI_DELTA, [1, 2], "[DONE]"),
    "event-int": _sse(OPENAI_DELTA, 5, "[DONE]"),
    "choices-int": _sse({"choices": 5}, "[DONE]"),
    "choice-int": _sse({"choices": [5]}, "[DONE]"),
    "delta-list": _sse({"choices": [{"delta": [1]}]}, "[DONE]"),
    "delta-content-object": _sse({"choices": [{"delta": {"content": {"a": 1}}}]}, "[DONE]"),
    "usage-list": _sse(OPENAI_DELTA, {"choices": [], "usage": [1]}, "[DONE]"),
}
for _name, _value in BAD_COUNTS.items():
    for _key in OPENAI_USAGE:
        _usage = {k: 1 for k in OPENAI_USAGE}
        _usage[_key] = _value
        OPENAI_STREAM_CASES[f"{_key}-{_name}"] = _sse(
            OPENAI_DELTA, _openai_usage_event(_usage), "[DONE]"
        )

ANTHROPIC_DELTA = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}


def _message_start(usage):
    return {"type": "message_start", "message": {"usage": usage}}


ANTHROPIC_STREAM_CASES = {
    "event-list": _sse(ANTHROPIC_DELTA, [1, 2]),
    "event-int": _sse(ANTHROPIC_DELTA, 5),
    "delta-list": _sse({"type": "content_block_delta", "delta": [1]}),
    "delta-text-object": _sse({"type": "content_block_delta", "delta": {"text": {"a": 1}}}),
    "message-list": _sse({"type": "message_start", "message": [1]}),
    "start-usage-list": _sse(_message_start([1])),
    "delta-usage-list": _sse({"type": "message_delta", "usage": [1]}),
}
for _name, _value in BAD_COUNTS.items():
    for _key in ANTHROPIC_USAGE:
        _usage = {k: 1 for k in ANTHROPIC_USAGE}
        _usage[_key] = _value
        ANTHROPIC_STREAM_CASES[f"start-{_key}-{_name}"] = _sse(_message_start(_usage))
    ANTHROPIC_STREAM_CASES[f"delta-output_tokens-{_name}"] = _sse(
        {"type": "message_delta", "usage": {"output_tokens": _value}}
    )


def _stream_matrix():
    rows = []
    for adapter_name, factory, cases in (
        ("openai", _openai, OPENAI_STREAM_CASES),
        ("openai_compatible", _compat, OPENAI_STREAM_CASES),
        ("anthropic", _anthropic, ANTHROPIC_STREAM_CASES),
    ):
        for case, raw in cases.items():
            rows.append(pytest.param(factory, raw, id=f"{adapter_name}-{case}"))
    return rows


async def _drain(adapter):
    return [c async for c in adapter.stream(model="m", messages=[USER])]


@pytest.mark.asyncio
@pytest.mark.parametrize("factory,raw", _stream_matrix())
async def test_hostile_stream_event_is_a_typed_provider_error(monkeypatch, factory, raw):
    _serve(monkeypatch, raw, "text/event-stream")
    with pytest.raises(AIProviderError) as exc:
        await _drain(factory())
    assert exc.value.code == "provider_unexpected_shape"


_STREAM_ADAPTERS = pytest.mark.parametrize(
    "factory,tail",
    [
        (_openai, ["[DONE]"]),
        (_compat, ["[DONE]"]),
        (_anthropic, []),
    ],
    ids=["openai", "openai_compatible", "anthropic"],
)


def _with_bad_line(factory, tail, bad_line: bytes) -> bytes:
    delta = OPENAI_DELTA if factory is not _anthropic else ANTHROPIC_DELTA
    return _sse(delta, bad_line, delta, *tail)


@pytest.mark.asyncio
@_STREAM_ADAPTERS
async def test_unparseable_stream_line_is_skipped(monkeypatch, factory, tail):
    """An SSE line that is not JSON is skipped, as before."""
    _serve(monkeypatch, _with_bad_line(factory, tail, b"{not json"), "text/event-stream")
    chunks = await _drain(factory())
    assert [c.delta_text for c in chunks if not c.done] == ["x", "x"]
    assert chunks[-1].done is True


@pytest.mark.asyncio
@_STREAM_ADAPTERS
async def test_deeply_nested_stream_line_never_escapes_untyped(monkeypatch, factory, tail):
    """Whether the runtime's parser hits its recursion limit (line skipped) or
    parses it (a non-object event, a typed error) depends on the interpreter;
    either way nothing untyped escapes."""
    _serve(monkeypatch, _with_bad_line(factory, tail, DEEP), "text/event-stream")
    try:
        chunks = await _drain(factory())
    except AIProviderError as exc:
        assert exc.code == "provider_unexpected_shape"
    else:
        assert chunks[-1].done is True


# ---------- negative token counts --------------------------------------------

NEG = -1_000_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory,body",
    [
        (_openai, _openai_body(usage={"prompt_tokens": NEG, "completion_tokens": NEG})),
        (_compat, _openai_body(usage={"prompt_tokens": NEG, "completion_tokens": NEG})),
        (_anthropic, _anthropic_body(usage={"input_tokens": NEG, "output_tokens": NEG})),
    ],
    ids=["openai", "openai_compatible", "anthropic"],
)
@pytest.mark.parametrize("path", ["chat", "chat_structured", "function_call"])
async def test_negative_token_counts_clamp_to_zero(monkeypatch, factory, body, path):
    _serve(monkeypatch, json.dumps(body).encode())
    adapter = factory()
    if path == "chat":
        resp = await adapter.chat(model="m", messages=[USER])
    elif path == "chat_structured":
        resp = await adapter.chat_structured(model="m", messages=[USER], schema=SCHEMA)
    else:
        resp = await adapter.function_call(model="m", messages=[USER], tools=TOOLS)
    assert (resp.prompt_tokens, resp.completion_tokens) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_openai, _compat], ids=["openai", "openai_compatible"])
async def test_negative_embed_token_count_clamps_to_zero(monkeypatch, factory):
    _serve(monkeypatch, json.dumps(_embed_body(usage={"prompt_tokens": NEG})).encode())
    resp = await factory().embed(texts=["a"], model="m")
    assert resp.prompt_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory,raw",
    [
        (
            _openai,
            _sse(OPENAI_DELTA, _openai_usage_event({"prompt_tokens": NEG, "completion_tokens": NEG}), "[DONE]"),
        ),
        (
            _compat,
            _sse(OPENAI_DELTA, _openai_usage_event({"prompt_tokens": NEG, "completion_tokens": NEG}), "[DONE]"),
        ),
        (
            _anthropic,
            _sse(
                _message_start({"input_tokens": NEG, "output_tokens": NEG}),
                ANTHROPIC_DELTA,
                {"type": "message_delta", "usage": {"output_tokens": NEG}},
            ),
        ),
    ],
    ids=["openai", "openai_compatible", "anthropic"],
)
async def test_negative_stream_token_counts_clamp_to_zero(monkeypatch, factory, raw):
    _serve(monkeypatch, raw, "text/event-stream")
    final = (await _drain(factory()))[-1].final_usage
    assert (final.prompt_tokens, final.completion_tokens) == (0, 0)


# ---------- the parser itself ------------------------------------------------


@pytest.mark.parametrize(
    "value,expected", [(None, 0), (0, 0), (7, 7), (7.0, 7), (-3, 0), (-2.0, 0), (10**9, 10**9)]
)
def test_token_count_accepts_integers_and_clamps_negatives(value, expected):
    assert token_count(value) == expected


@pytest.mark.parametrize("value", list(BAD_COUNTS.values()), ids=list(BAD_COUNTS))
def test_token_count_rejects_everything_else(value):
    with pytest.raises((TypeError, ValueError, OverflowError)):
        token_count(value)


# ---------- the echoed model name ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("echoed", [{"a": 1}, [1], 5, "", "m" * 500])
async def test_unusable_echoed_model_falls_back_to_the_requested_one(monkeypatch, echoed):
    _serve(monkeypatch, json.dumps(_openai_body(model=echoed)).encode())
    resp = await _openai().chat(model="gpt-4o-mini", messages=[USER])
    assert resp.model == "gpt-4o-mini"
