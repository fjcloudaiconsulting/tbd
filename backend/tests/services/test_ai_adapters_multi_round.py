"""Multi-round tool calling through the provider adapters (TBD-579).

Each adapter runs a two-round tool conversation against a fixture in the
provider's wire format (``tests/fixtures/ai_providers``): round 1 returns
tool calls, the caller appends the provider-neutral transcript (the
assistant turn with call ids plus one ``tool`` message per call), and
round 2 must reach the provider in its native shape with every result
keyed to the id the adapter returned in round 1.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import (
    OpenAICompatibleAdapter,
)
from app.services.ai_token_estimate import estimate_prompt_tokens_from_messages


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ai_providers"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    for name in ("budgets_list", "forecast_get")
]

SYSTEM = {"role": "system", "content": "You are the budgeting assistant."}
USER = {"role": "user", "content": "How are my budgets doing?"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _install_sequence(monkeypatch, responses: list[dict]) -> list[dict]:
    """Serve ``responses`` in order; return the captured JSON request bodies."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=responses[len(bodies) - 1])

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return bodies


def _results_for(tool_calls: list[dict]) -> list[dict]:
    return [
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps({"tool": call["name"], "ok": True}),
        }
        for call in tool_calls
    ]


async def _two_rounds(adapter, model: str, monkeypatch, fixture: dict):
    bodies = _install_sequence(monkeypatch, [fixture["round1"], fixture["round2"]])
    transcript = [SYSTEM, USER]
    first = await adapter.function_call(model=model, messages=transcript, tools=TOOLS)
    transcript = transcript + [
        {"role": "assistant", "content": first.content, "tool_calls": first.tool_calls},
        *_results_for(first.tool_calls),
    ]
    second = await adapter.function_call(model=model, messages=transcript, tools=TOOLS)
    return first, second, bodies


# ---------- Anthropic ------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_two_round_tool_conversation(monkeypatch):
    fixture = _fixture("anthropic_two_round.json")
    first, second, bodies = await _two_rounds(
        AnthropicAdapter(api_key="sk-ant-test"), "claude-haiku-4-5", monkeypatch, fixture
    )

    wire_uses = [b for b in fixture["round1"]["content"] if b["type"] == "tool_use"]
    assert first.tool_calls == [
        {"id": b["id"], "name": b["name"], "arguments": b["input"]} for b in wire_uses
    ]

    sent = bodies[1]
    assert sent["system"] == SYSTEM["content"]
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assistant = sent["messages"][1]["content"]
    assert assistant[0] == {"type": "text", "text": first.content}
    assert assistant[1:] == [
        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
        for c in first.tool_calls
    ]
    # Both results in ONE user message, in call order, keyed by the round-1 ids.
    results = sent["messages"][2]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert [r["tool_use_id"] for r in results] == [c["id"] for c in first.tool_calls]
    assert [r["content"] for r in results] == [
        m["content"] for m in _results_for(first.tool_calls)
    ]

    assert second.tool_calls == []
    assert second.content == fixture["round2"]["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("assistant_text", ["", "\n\n"])
async def test_anthropic_folds_following_user_text_after_tool_results(
    monkeypatch, assistant_text
):
    fixture = _fixture("anthropic_two_round.json")
    bodies = _install_sequence(monkeypatch, [fixture["round2"]])
    call = {"id": "toolu_x", "name": "budgets_list", "arguments": {}}
    await AnthropicAdapter(api_key="k").function_call(
        model="claude-haiku-4-5",
        messages=[
            USER,
            {"role": "assistant", "content": assistant_text, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "toolu_x", "content": "[]"},
            {"role": "user", "content": "And last month?"},
        ],
        tools=TOOLS,
    )
    messages = bodies[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # Blank assistant text never becomes a text block (Anthropic 400s on it).
    assert messages[1]["content"] == [
        {"type": "tool_use", "id": "toolu_x", "name": "budgets_list", "input": {}}
    ]
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "[]"},
        {"type": "text", "text": "And last month?"},
    ]


def _three_rounds_transcript() -> list[dict]:
    """user, assistant(call a), tool a, assistant(call b), tool b, then a
    plain answer replayed with an empty tool_calls list, then a user turn."""
    a = {"id": "call_a", "name": "budgets_list", "arguments": {}}
    b = {"id": "call_b", "name": "forecast_get", "arguments": {"period_start": "2026-09-01"}}
    return [
        SYSTEM,
        USER,
        {"role": "assistant", "content": "", "tool_calls": [a]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ra"},
        {"role": "assistant", "content": "", "tool_calls": [b]},
        {"role": "tool", "tool_call_id": "call_b", "content": "rb"},
        {"role": "assistant", "content": "All good.", "tool_calls": []},
        {"role": "user", "content": "Thanks, and next month?"},
    ]


@pytest.mark.asyncio
async def test_anthropic_keeps_each_round_results_after_its_own_tool_use(monkeypatch):
    bodies = _install_sequence(
        monkeypatch, [_fixture("anthropic_two_round.json")["round2"]]
    )
    await AnthropicAdapter(api_key="k").function_call(
        model="claude-haiku-4-5", messages=_three_rounds_transcript(), tools=TOOLS
    )
    messages = bodies[0]["messages"]
    assert [m["role"] for m in messages] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user"
    ]
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_a", "content": "ra"}
    ]
    assert messages[4]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_b", "content": "rb"}
    ]
    # A replayed plain answer drops the empty tool_calls key (Anthropic 400s on it).
    assert messages[5] == {"role": "assistant", "content": "All good."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wire_ids", "kept_first"),
    [(["toolu_dup", "toolu_dup"], True), (["x" * 65, "toolu_ok"], False)],
)
async def test_anthropic_unsafe_or_repeated_ids_are_replaced(
    monkeypatch, wire_ids, kept_first
):
    round1 = _fixture("anthropic_two_round.json")["round1"]
    round1["content"][1]["id"], round1["content"][2]["id"] = wire_ids
    _install_sequence(monkeypatch, [round1])
    resp = await AnthropicAdapter(api_key="k").function_call(
        model="claude-haiku-4-5", messages=[USER], tools=TOOLS
    )
    ids = [c["id"] for c in resp.tool_calls]
    assert (ids[0] == wire_ids[0]) is kept_first
    assert all(len(i) <= 64 for i in ids) and len(set(ids)) == 2


# ---------- OpenAI and OpenAI-compatible -----------------------------


def _assert_openai_round_two(sent: dict, first) -> None:
    assert sent["messages"][:2] == [SYSTEM, USER]
    assistant = sent["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    assert assistant["tool_calls"] == [
        {
            "id": c["id"],
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
        }
        for c in first.tool_calls
    ]
    tool_messages = sent["messages"][3:]
    assert [m["role"] for m in tool_messages] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == [c["id"] for c in first.tool_calls]


def _expected_openai_calls(fixture: dict) -> list[dict]:
    return [
        {
            "id": c["id"],
            "name": c["function"]["name"],
            "arguments": json.loads(c["function"]["arguments"]),
        }
        for c in fixture["round1"]["choices"][0]["message"]["tool_calls"]
    ]


@pytest.mark.asyncio
async def test_openai_two_round_tool_conversation(monkeypatch):
    fixture = _fixture("openai_two_round.json")
    first, second, bodies = await _two_rounds(
        OpenAIAdapter(api_key="sk-test"), "gpt-4o-mini", monkeypatch, fixture
    )
    assert first.tool_calls == _expected_openai_calls(fixture)
    _assert_openai_round_two(bodies[1], first)
    assert second.tool_calls == []
    assert second.content == fixture["round2"]["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_openrouter_two_round_tool_conversation(monkeypatch):
    fixture = _fixture("openrouter_two_round.json")
    first, second, bodies = await _two_rounds(
        OpenAICompatibleAdapter(api_key="sk-or-test", base_url="https://openrouter.ai/api"),
        "anthropic/claude-haiku-4.5",
        monkeypatch,
        fixture,
    )
    # OpenRouter's passed-through upstream ids are kept, never replaced.
    assert first.tool_calls == _expected_openai_calls(fixture)
    _assert_openai_round_two(bodies[1], first)
    assert second.content == fixture["round2"]["choices"][0]["message"]["content"]


def _compat_round(tool_calls: list[dict]) -> dict:
    return {
        "model": "local",
        "choices": [{"message": {"content": None, "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.mark.asyncio
async def test_compat_server_without_ids_gets_unique_ids(monkeypatch):
    call = {"function": {"name": "budgets_list", "arguments": "{}"}}
    _install_sequence(
        monkeypatch, [_compat_round([call, call]), _compat_round([call])]
    )
    adapter = OpenAICompatibleAdapter(api_key="k", base_url="https://compat.example.org")
    first = await adapter.function_call(model="local", messages=[USER], tools=TOOLS)
    second = await adapter.function_call(model="local", messages=[USER], tools=TOOLS)
    ids = [c["id"] for c in first.tool_calls + second.tool_calls]
    assert all(isinstance(i, str) and i for i in ids)
    assert len(set(ids)) == 3


@pytest.mark.asyncio
async def test_anthropic_drops_blank_user_text_after_tool_results(monkeypatch):
    bodies = _install_sequence(
        monkeypatch, [_fixture("anthropic_two_round.json")["round2"]]
    )
    call = {"id": "toolu_x", "name": "budgets_list", "arguments": {}}
    await AnthropicAdapter(api_key="k").function_call(
        model="claude-haiku-4-5",
        messages=[
            USER,
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "toolu_x", "content": "[]"},
            {"role": "user", "content": "  "},
        ],
        tools=TOOLS,
    )
    assert bodies[0]["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "[]"}
    ]


@pytest.mark.asyncio
async def test_compat_duplicate_ids_in_one_response_are_replaced(monkeypatch):
    call = {"id": "0", "function": {"name": "budgets_list", "arguments": "{}"}}
    _install_sequence(monkeypatch, [_compat_round([call, call])])
    resp = await OpenAICompatibleAdapter(
        api_key="k", base_url="https://compat.example.org"
    ).function_call(model="local", messages=[USER], tools=TOOLS)
    assert resp.tool_calls[0]["id"] == "0"
    assert resp.tool_calls[1]["id"] not in ("", "0")


@pytest.mark.asyncio
async def test_compat_object_arguments_are_kept(monkeypatch):
    call = {"id": "c1", "function": {"name": "forecast_get", "arguments": {"period_start": "2026-09-01"}}}
    _install_sequence(monkeypatch, [_compat_round([call])])
    resp = await OpenAICompatibleAdapter(
        api_key="k", base_url="https://compat.example.org"
    ).function_call(model="local", messages=[USER], tools=TOOLS)
    assert resp.tool_calls[0]["arguments"] == {"period_start": "2026-09-01"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _compat_round(["not-a-call"]),
        _compat_round([{"id": "c1", "function": "x"}]),
        {"choices": [{"message": "x"}]},
    ],
)
async def test_malformed_tool_calls_are_a_typed_provider_error(monkeypatch, response):
    _install_sequence(monkeypatch, [response])
    with pytest.raises(AIProviderError) as exc:
        await OpenAIAdapter(api_key="k").function_call(
            model="gpt-4o-mini", messages=[USER], tools=TOOLS
        )
    assert exc.value.code == "provider_unexpected_shape"


@pytest.mark.asyncio
async def test_hostile_ids_names_and_arguments_are_normalized(monkeypatch):
    call = {
        "id": "id with spaces",
        "function": {"name": ["x"], "arguments": "[" * 100_000},
    }
    _install_sequence(monkeypatch, [_compat_round([call])])
    resp = await OpenAICompatibleAdapter(
        api_key="k", base_url="https://compat.example.org"
    ).function_call(model="local", messages=[USER], tools=TOOLS)
    (parsed,) = resp.tool_calls
    assert parsed["id"] != "id with spaces" and parsed["id"]
    assert parsed["name"] == "['x']"
    assert parsed["arguments"] == {}


@pytest.mark.asyncio
async def test_openai_three_round_transcript(monkeypatch):
    bodies = _install_sequence(
        monkeypatch, [_fixture("openai_two_round.json")["round2"]]
    )
    await OpenAIAdapter(api_key="k").function_call(
        model="gpt-4o-mini", messages=_three_rounds_transcript(), tools=TOOLS
    )
    messages = bodies[0]["messages"]
    assert [m["role"] for m in messages] == [
        "system", "user", "assistant", "tool", "assistant", "tool", "assistant", "user"
    ]
    assert [m["tool_calls"][0]["id"] for m in (messages[2], messages[4])] == [
        "call_a", "call_b"
    ]
    # OpenAI rejects an empty tool_calls array.
    assert messages[6] == {"role": "assistant", "content": "All good."}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_args", ["[]", "null", "\"x\"", "not json"])
async def test_openai_non_object_arguments_become_empty_dict(monkeypatch, raw_args):
    call = {"id": "call_1", "function": {"name": "budgets_list", "arguments": raw_args}}
    _install_sequence(monkeypatch, [_compat_round([call])])
    resp = await OpenAIAdapter(api_key="k").function_call(
        model="gpt-4o-mini", messages=[USER], tools=TOOLS
    )
    assert resp.tool_calls == [{"id": "call_1", "name": "budgets_list", "arguments": {}}]


@pytest.mark.asyncio
async def test_plain_messages_reach_every_provider_unchanged(monkeypatch):
    anthropic = _fixture("anthropic_two_round.json")["round2"]
    openai = _fixture("openai_two_round.json")["round2"]
    bodies = _install_sequence(monkeypatch, [anthropic, openai, openai])
    await AnthropicAdapter(api_key="k").function_call(
        model="claude-haiku-4-5", messages=[SYSTEM, USER], tools=TOOLS
    )
    await OpenAIAdapter(api_key="k").function_call(
        model="gpt-4o-mini", messages=[SYSTEM, USER], tools=TOOLS
    )
    await OpenAICompatibleAdapter(api_key="k", base_url="https://c.example.org").function_call(
        model="m", messages=[SYSTEM, USER], tools=TOOLS
    )
    assert bodies[0]["messages"] == [USER]
    assert bodies[1]["messages"] == [SYSTEM, USER]
    assert bodies[2]["messages"] == [SYSTEM, USER]


# ---------- Cap projection -------------------------------------------


def test_estimator_counts_assistant_tool_calls():
    calls = [{"id": "call_1", "name": "forecast_get", "arguments": {"period_start": "2026-09-01"}}]
    turn = {"role": "assistant", "content": None, "tool_calls": calls}
    # At least one token per 4 serialized chars of the calls.
    assert estimate_prompt_tokens_from_messages([turn]) >= len(json.dumps(calls)) // 4


def test_estimator_never_raises_on_unserializable_tool_calls():
    turn = {"role": "assistant", "content": "hi", "tool_calls": [{"arguments": object()}]}
    assert estimate_prompt_tokens_from_messages([turn]) >= 1


# ---------- Hostile bodies and replayed blank answers ------------------


def _raw_sequence(monkeypatch, raw: bytes) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
    original = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)


def _adapters():
    return [
        (AnthropicAdapter(api_key="k"), "claude-haiku-4-5"),
        (OpenAIAdapter(api_key="k"), "gpt-4o-mini"),
        (OpenAICompatibleAdapter(api_key="k", base_url="https://c.example.org"), "m"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"[" * 100_000 + b"]" * 100_000,
        b'{"choices":[{"message":{"content":"x"}}],"content":[],"usage":{"prompt_tokens":"abc","input_tokens":"abc"}}',
        b'{"choices":[{"message":{"content":"x"}}],"content":[],"usage":{"prompt_tokens":Infinity,"input_tokens":Infinity}}',
        b'{"choices":[{"message":{"content":"x"}}],"content":[],"usage":[1]}',
        b"[1, 2]",
    ],
    ids=["deep-nesting", "non-numeric-usage", "infinite-usage", "list-usage", "list-payload"],
)
async def test_hostile_bodies_end_in_a_typed_provider_error(monkeypatch, raw):
    _raw_sequence(monkeypatch, raw)
    for adapter, model in _adapters():
        with pytest.raises(AIProviderError):
            await adapter.function_call(model=model, messages=[USER], tools=TOOLS)


@pytest.mark.asyncio
async def test_upstream_id_format_with_dots_and_colons_is_kept(monkeypatch):
    call = {"id": "functions.budgets_list:0", "function": {"name": "budgets_list", "arguments": "{}"}}
    _install_sequence(monkeypatch, [_compat_round([call])])
    resp = await OpenAICompatibleAdapter(
        api_key="k", base_url="https://compat.example.org"
    ).function_call(model="local", messages=[USER], tools=TOOLS)
    assert resp.tool_calls[0]["id"] == "functions.budgets_list:0"


@pytest.mark.asyncio
async def test_replayed_blank_answer_is_valid_for_each_provider(monkeypatch):
    blank = {"role": "assistant", "content": None, "tool_calls": []}
    bodies = _install_sequence(
        monkeypatch,
        [
            _fixture("anthropic_two_round.json")["round2"],
            _fixture("openai_two_round.json")["round2"],
        ],
    )
    await AnthropicAdapter(api_key="k").function_call(
        model="claude-haiku-4-5", messages=[USER, blank, USER], tools=TOOLS
    )
    await OpenAIAdapter(api_key="k").function_call(
        model="gpt-4o-mini", messages=[USER, blank, USER], tools=TOOLS
    )
    assert [m["role"] for m in bodies[0]["messages"]] == ["user", "user"]
    assert bodies[1]["messages"][1] == {"role": "assistant", "content": ""}


@pytest.mark.asyncio
async def test_non_string_content_becomes_empty(monkeypatch):
    reply = {"choices": [{"message": {"content": [{"x": 1}]}}], "usage": {}}
    _install_sequence(monkeypatch, [reply, reply])
    for adapter in (
        OpenAIAdapter(api_key="k"),
        OpenAICompatibleAdapter(api_key="k", base_url="https://c.example.org"),
    ):
        resp = await adapter.function_call(model="m", messages=[USER], tools=TOOLS)
        assert resp.content == ""
