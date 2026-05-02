"""Tests for require_feature factory-time validation.

A typo'd key must fail at module import time, not at first request.
"""
import pytest

from app.auth.feature_deps import require_feature
from app.services.feature_service import UnknownFeatureKey


def test_require_feature_unknown_key_raises_at_factory_time():
    with pytest.raises(UnknownFeatureKey):
        require_feature("ai.totally_made_up")


def test_require_feature_known_key_returns_callable():
    dep = require_feature("ai.autocategorize")
    assert callable(dep)
