"""Tests for plan_service.canonicalize_features."""
import pytest

from app.auth.feature_catalog import ALL_FEATURE_KEYS
from app.services.exceptions import ValidationError
from app.services.plan_service import canonicalize_features


def test_canonicalize_partial_merges_with_existing():
    existing = {"ai.budget": True, "ai.forecast": False, "ai.smart_plan": False, "ai.autocategorize": False}
    out = canonicalize_features({"ai.forecast": True}, existing=existing)
    assert out == {
        "ai.budget": True,
        "ai.forecast": True,
        "ai.smart_plan": False,
        "ai.autocategorize": False,
    }


def test_canonicalize_full_overwrite_no_existing():
    out = canonicalize_features({"ai.budget": True})
    assert set(out.keys()) == ALL_FEATURE_KEYS
    assert out["ai.budget"] is True
    assert out["ai.forecast"] is False
    assert out["ai.smart_plan"] is False
    assert out["ai.autocategorize"] is False


def test_canonicalize_unknown_key_raises():
    with pytest.raises(ValidationError) as exc:
        canonicalize_features({"ai.totally_made_up": True})
    assert "ai.totally_made_up" in exc.value.detail


def test_canonicalize_strict_bool_rejects_string():
    with pytest.raises(Exception):  # Pydantic ValidationError
        canonicalize_features({"ai.budget": "true"})


def test_canonicalize_strict_bool_rejects_int():
    with pytest.raises(Exception):
        canonicalize_features({"ai.budget": 1})


def test_canonicalize_returns_alias_keys():
    out = canonicalize_features({"ai.budget": True})
    # Must be dotted-keys (the storage shape), not snake_case.
    assert "ai.budget" in out
    assert "ai_budget" not in out
