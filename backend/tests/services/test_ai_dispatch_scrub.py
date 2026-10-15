"""TBD-628: everything an AI provider receives is scrubbed first.

D1-D5 on the dispatch layer (real adapters over the fake provider of the platform
dispatch tests), B1/B2 on the two JSON builders.
"""
from __future__ import annotations

import ast
import copy
import datetime
import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import settings as app_settings
from app.models.org_ai_caps import OrgAIDefaultCaps
from app.models.org_ai_consent import OrgAIConsent
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting
from app.services import ai_dispatch, budget_rebalance_service as brs, ai_forecast_refine_service as afr
from app.services.ai_credential_crypto import encrypt
from app.services.ai_forecast_refine_token_estimate import Scope

# Fixtures and helpers of the platform dispatch suite (sf, db, fake, _env, ...).
from tests.services.test_ai_dispatch_platform import (  # noqa: F401
    Fake, _env, _json_content, fake, db, sf, mk_org, set_platform, setup, SCHEMA, TOOLS,
)


APP = Path(__file__).resolve().parents[2] / "app"
ORIGINALS = ["NL91ABNA0417164300", "jan@example.nl", "+31 6 12345678", "4111 1111 1111 1111"]
S = ("pay NL91ABNA0417164300, mail jan@example.nl, call +31 6 12345678, "
     "card 4111 1111 1111 1111, total 1,234.56 on 2026-10-15")
MSGS = [{"role": "user", "content": S}]


def _clean(blob: str):
    for o in ORIGINALS:
        assert o not in blob, o
    for p in ("[IBAN]", "[EMAIL]", "[PHONE]", "[CARD]"):
        assert p in blob, p
    assert "1,234.56" in blob and "2026-10-15" in blob


async def _byok(db):
    await set_platform(db)
    org = await mk_org(db)
    cred = OrgAICredential(org_id=org, provider=AiProvider.OPENAI, encrypted_api_key=encrypt("sk-byok"),
                          discovered_capabilities=["chat", "embed", "structured_output", "function_call", "stream"])
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=org, credential_id=cred.id, model="gpt-4o-mini"))
    db.add(OrgAIConsent(org_id=org, consent_version=app_settings.ai_native_current_consent_version))
    db.add(OrgAIDefaultCaps(org_id=org, soft_cap_cents=None, hard_cap_cents=100_000, period="monthly"))
    await db.commit()
    return org


async def _org(db, kind):
    return await _byok(db) if kind == "byok" else (await setup(db))[0]


def _calls(msgs):
    d = ai_dispatch
    return {
        "call_llm": lambda db, o: d.call_llm(db, org_id=o, feature_key="chat",
                                             request_payload={"messages": msgs, "max_tokens": 100}),
        "structured": lambda db, o: d.call_llm_structured(db, org_id=o, feature_key="chat", messages=msgs,
                                                          response_schema=SCHEMA, max_tokens=100),
        "embed": lambda db, o: d.call_llm_embed(db, org_id=o, feature_key="chat", texts=[S],
                                                model="text-embedding-3-small"),
        "function": lambda db, o: d.call_llm_function(db, org_id=o, feature_key="chat", messages=msgs,
                                                      tools=TOOLS, max_tokens=100),
        "stream": lambda db, o: _drain(d.call_llm_stream(db, org_id=o, feature_key="chat", messages=msgs,
                                                         max_tokens=100)),
    }


async def _drain(gen):
    return [c async for c in gen]


@pytest.fixture
def spies(monkeypatch):
    est, prep = [], []
    real_est = ai_dispatch.estimate_prompt_tokens_from_messages
    real_prep = ai_dispatch._prepare_dispatch

    def e(messages, *a, **k):
        est.append(copy.deepcopy(messages))
        return real_est(messages, *a, **k)

    async def p(*a, **k):
        prep.append(copy.deepcopy(k.get("messages")))
        return await real_prep(*a, **k)

    monkeypatch.setattr(ai_dispatch, "estimate_prompt_tokens_from_messages", e)
    monkeypatch.setattr(ai_dispatch, "_prepare_dispatch", p)
    return est, prep


@pytest.mark.parametrize("kind", ["byok", "platform"])
@pytest.mark.parametrize("entry", ["call_llm", "structured", "embed", "function", "stream"])
async def test_d1_every_wrapper_scrubs_before_estimate_gate_and_adapter(db, fake, spies, kind, entry):
    est, prep = spies
    org = await _org(db, kind)
    await _calls(copy.deepcopy(MSGS))[entry](db, org)
    assert len(est) >= 1 and len(prep) == 1 and len(fake.bodies) >= 1
    for rec in (*est, *prep, *fake.bodies):
        _clean(json.dumps(rec, ensure_ascii=False))


async def test_d2_structured_retries_are_scrubbed(db, fake):
    org = await _org(db, "platform")
    fake.contents = ["nope", "nope", '{"a": 1}']
    await _calls(copy.deepcopy(MSGS))["structured"](db, org)
    assert len(fake.bodies) == 3
    for b in fake.bodies:
        _clean(json.dumps(b, ensure_ascii=False))


@pytest.mark.parametrize("entry", ["call_llm", "structured", "function", "stream", "embed"])
async def test_d3_caller_data_is_not_mutated(db, fake, entry):
    org = await _org(db, "platform")
    msgs = copy.deepcopy(MSGS)
    await _calls(msgs)[entry](db, org)
    assert msgs == MSGS and msgs[0]["content"] == S


async def test_d4_logs_hold_no_original(db, fake, caplog):
    caplog.set_level(logging.DEBUG)
    org = await _org(db, "platform")
    await _calls(copy.deepcopy(MSGS))["call_llm"](db, org)
    fake.status = 500
    with pytest.raises(Exception):
        await _calls(copy.deepcopy(MSGS))["call_llm"](db, org)
    assert caplog.records
    for r in caplog.records:
        blob = r.getMessage() + repr(r.__dict__)
        for o in ORIGINALS:
            assert o not in blob


# ---- D5: AST inventory ---------------------------------------------------------

_SEND = {"chat", "chat_structured", "embed", "function_call", "stream"}
_HTTP = {"request", "db", "client"}
PINNED = [
    "agent_chat.preflight", "ai_dispatch.call_llm", "ai_dispatch.call_llm_embed",
    "ai_dispatch.call_llm_function", "ai_dispatch.call_llm_stream", "ai_dispatch.call_llm_structured",
]


def _is_send(c: ast.Call) -> bool:
    f = c.func
    if isinstance(f, ast.Name):
        return f.id == "_prepare_dispatch"
    if isinstance(f, ast.Attribute):
        if f.attr == "_prepare_dispatch":
            return True
        recv = f.value.id if isinstance(f.value, ast.Name) else None
        return f.attr in _SEND and recv not in _HTTP
    return False


def _is_scrub(c: ast.Call) -> bool:
    f = c.func
    return (isinstance(f, ast.Attribute) and f.attr in {"scrub_messages", "scrub_text"}
            and isinstance(f.value, ast.Name) and f.value.id == "ai_scrub")


def _inventory():
    out = {}
    for path in APP.rglob("*.py"):
        if "ai_providers" in path.relative_to(APP).parts:
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
            sends = [c.lineno for c in calls if _is_send(c)]
            if sends:
                out[f"{path.stem}.{fn.name}"] = (min(sends), [c.lineno for c in calls if _is_scrub(c)])
    return out


def test_d5_every_content_sending_site_scrubs_first():
    inv = _inventory()
    assert sorted(inv) == sorted(PINNED)
    for name, (first_send, scrubs) in inv.items():
        assert scrubs and min(scrubs) < first_send, name


# ---- B1 / B2: builders that json.dumps user-writable names ------------------------

NAMES = {1: "Huur\nNL91ABNA0417164300", 2: "josé.garcia@example.es"}


def _check_builder_json(blob: str):
    assert "NL91ABNA0417164300" not in blob and "josé.garcia@example.es" not in blob
    assert "[EMAIL]" in blob and "[IBAN]" in blob and "\\u00e9" not in blob.replace("[EMAIL]", "")


def test_b1_forecast_refine_builder():
    baseline = {
        "period_start": "2026-10-01", "period_end": "2026-10-31", "forecast_income": "1", "forecast_expense": "2",
        "categories": [{"category_id": i, "category_name": n} for i, n in NAMES.items()],
    }
    history = [{"category_id": i, "month": "2026-09", "total_expense": 10.0 * i} for i in NAMES]
    msgs, _, _ = afr._build_refine_prompt(baseline=baseline, history=history, category_index=NAMES,
                                          timeframe_months=3, scope=Scope.ALL)
    content = msgs[1]["content"]
    data = json.loads(content)
    assert "Huur\n[IBAN]" in {h["category_name"] for h in data["history"]}
    _check_builder_json(content)


def test_b2_budget_rebalance_builder():
    facts = [
        brs._CategoryFact(category_id=i, category_name=n, budget_amount=Decimal("10"),
                          last_3mo_total=Decimal("30"), last_3mo_avg=Decimal("10"), current_mo_actual=Decimal("5"))
        for i, n in NAMES.items()
    ]
    msgs = brs._build_messages(facts, datetime.date(2026, 10, 1))
    content = msgs[1]["content"]
    agg = json.loads(content.split("AGGREGATES:\n", 1)[1])
    assert "Huur\n[IBAN]" in {c["category_name"] for c in agg["categories"]}
    _check_builder_json(content)
