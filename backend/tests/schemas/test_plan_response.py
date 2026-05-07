"""PlanResponse / PlanCreate / PlanUpdate validation contract.

Verifies features canonicalization on read, and that legacy ai_* keys
on write are rejected by extra="forbid".
"""
import pytest
from pydantic import ValidationError

from app.schemas.subscription import PlanCreate, PlanResponse, PlanUpdate


def test_plan_response_features_canonicalizes_missing_keys():
    """If storage somehow drifts, response still emits the canonical shape."""
    plan = PlanResponse(
        id=1, name="X", slug="x", description="", is_custom=False,
        is_active=True, sort_order=0,
        price_monthly=0, price_yearly=0,
        max_users=None, retention_days=None,
        features={"ai.budget": True},  # incomplete on purpose
    )
    out = plan.model_dump()["features"]
    assert out["ai.budget"] is True
    assert out["ai.forecast"] is False
    assert out["ai.smart_plan"] is False
    assert out["ai.autocategorize"] is False


def test_plan_response_features_full_payload():
    plan = PlanResponse(
        id=1, name="Pro", slug="pro", description="", is_custom=False,
        is_active=True, sort_order=1,
        price_monthly=10, price_yearly=100,
        max_users=None, retention_days=None,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": True,
            "ai.autocategorize": False,
        },
    )
    payload = plan.model_dump()
    assert payload["features"]["ai.budget"] is True
    assert payload["features"]["ai.forecast"] is False
    assert payload["features"]["ai.smart_plan"] is True
    assert payload["features"]["ai.autocategorize"] is False
    # Legacy ai_*_enabled keys are gone.
    assert "ai_budget_enabled" not in payload
    assert "ai_forecast_enabled" not in payload
    assert "ai_smart_plan_enabled" not in payload


def test_plan_create_rejects_legacy_ai_payload():
    with pytest.raises(ValidationError):
        PlanCreate.model_validate({
            "name": "X", "slug": "x",
            "ai_budget_enabled": True,  # legacy field; extra="forbid" rejects
        })


def test_plan_update_rejects_legacy_ai_payload():
    with pytest.raises(ValidationError):
        PlanUpdate.model_validate({"ai_budget_enabled": True})


def test_plan_create_accepts_features_partial():
    pc = PlanCreate.model_validate({
        "name": "X", "slug": "x",
        "features": {"ai.budget": True},
    })
    assert pc.features == {"ai.budget": True}
