import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services import budget_rebalance_service as svc
from app.services.budget_rebalance_service import _CategoryFact


def _fact(cid, name, budget, avg, actual):
    return _CategoryFact(
        category_id=cid,
        category_name=name,
        budget_amount=Decimal(budget),
        last_3mo_total=Decimal(avg) * 3,
        last_3mo_avg=Decimal(avg),
        current_mo_actual=Decimal(actual),
    )


class _Period:
    start_date = datetime.date(2026, 6, 1)
    end_date = None


def test_parse_ai_guidance_filters_unknown_ids():
    priority, reasons, summary = svc._parse_ai_guidance(
        {
            "priority": [2, 999],
            "summary": "ok",
            "reasoning": [{"category_id": 2, "text": "rent matters"}],
        },
        allowed_ids={1, 2},
    )
    assert priority == [2]
    assert reasons[2] == "rent matters"
    assert summary == "ok"


def test_parse_ai_guidance_survives_garbage():
    # A non-dict / missing keys must degrade to empty guidance, never raise.
    priority, reasons, summary = svc._parse_ai_guidance(
        {"priority": "nope", "reasoning": ["x", {"category_id": 5}]},
        allowed_ids={1, 2},
    )
    assert priority == []
    assert reasons == {}
    assert summary == ""


@pytest.mark.asyncio
async def test_suggest_rebalance_is_zero_sum(monkeypatch):
    facts = [
        _fact(1, "Transportation", "100", "90", "90"),
        _fact(2, "Bills", "90", "100", "100"),
    ]
    monkeypatch.setattr(svc, "get_current_period", AsyncMock(return_value=_Period))
    monkeypatch.setattr(svc, "_gather_facts", AsyncMock(return_value=facts))

    class _Resp:
        parsed = {
            "priority": [2],
            "summary": "Shift to bills",
            "reasoning": [{"category_id": 2, "text": "covers rent"}],
        }

    class _Result:
        response = _Resp()

    monkeypatch.setattr(svc, "call_llm_structured", AsyncMock(return_value=_Result))

    out = await svc.suggest_rebalance(db=AsyncMock(), org_id=1)
    assert out.status == "ok"
    assert out.is_balanced is True
    assert out.total_budget == Decimal("190.00")
    assert out.total_suggested == Decimal("190.00")
    assert out.uncovered_overspend == Decimal("0.00")
    by_cat = {s.category_id: s for s in out.suggestions}
    assert by_cat[1].suggested_amount == Decimal("90.00")
    assert by_cat[2].suggested_amount == Decimal("100.00")


@pytest.mark.asyncio
async def test_suggest_rebalance_refuses_when_no_surplus(monkeypatch):
    facts = [
        _fact(1, "Transportation", "100", "120", "120"),  # over
        _fact(2, "Bills", "90", "100", "100"),            # over
    ]
    monkeypatch.setattr(svc, "get_current_period", AsyncMock(return_value=_Period))
    monkeypatch.setattr(svc, "_gather_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(svc, "call_llm_structured", AsyncMock())

    out = await svc.suggest_rebalance(db=AsyncMock(), org_id=1)
    assert out.status == "empty_no_surplus"
    assert out.suggestions == []
    assert out.total_budget == Decimal("190.00")
    assert out.is_balanced is True
    # The LLM is never called when there is nothing to reallocate.
    svc.call_llm_structured.assert_not_called()


@pytest.mark.asyncio
async def test_suggest_rebalance_stays_balanced_when_llm_unavailable(monkeypatch):
    # A dispatch failure must NOT abort the rebalance: the deterministic
    # allocator still produces a balanced suggestion offline.
    facts = [
        _fact(1, "Transportation", "100", "90", "90"),
        _fact(2, "Bills", "90", "100", "100"),
    ]
    monkeypatch.setattr(svc, "get_current_period", AsyncMock(return_value=_Period))
    monkeypatch.setattr(svc, "_gather_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(
        svc,
        "call_llm_structured",
        AsyncMock(side_effect=svc.AIDispatchFailed("boom")),
    )

    out = await svc.suggest_rebalance(db=AsyncMock(), org_id=1)
    assert out.status == "ok"
    assert out.is_balanced is True
    assert out.total_suggested == out.total_budget == Decimal("190.00")
    by_cat = {s.category_id: s for s in out.suggestions}
    assert by_cat[1].suggested_amount == Decimal("90.00")
    assert by_cat[2].suggested_amount == Decimal("100.00")
