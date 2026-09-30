"""Unit tests for ``app.rate_limit_overrides``: the limit-string
formatter and the zero-arg provider returned by ``dynamic_limit``.

The request-path fences (override actually enforced by the limiter) live in
``test_rate_limit_overrides_wiring.py``.
"""
from __future__ import annotations

import pytest

from app.rate_limit_overrides import (
    dynamic_limit,
    format_limit,
)


def test_dynamic_limit_validates_default_at_construction():
    """An unparseable default raises immediately so a typo in a
    decorator argument crashes import, not the first request.
    """
    with pytest.raises(ValueError):
        dynamic_limit("auth.login", "20/forever")


def _provider_in_ctx(value, pattern="auth.resend_verification", default="3/hour"):
    import contextvars

    from app.rate_limit_overrides import _overrides_cv

    def run():
        if value is not None:
            _overrides_cv.set(value)
        return dynamic_limit(pattern, default)()

    return contextvars.copy_context().run(run)


def test_f8_provider_empty_context_returns_default():
    assert _provider_in_ctx(None) == "3/hour"
    assert _provider_in_ctx({"other.pattern": "1/minute"}) == "3/hour"


def test_f8_provider_returns_override_for_its_pattern():
    assert _provider_in_ctx({"auth.resend_verification": "9/minute"}) == "9/minute"


@pytest.mark.parametrize(
    "bad", ["nonsense", "2/45", "0/minute", "", "-1/minute"]
)
def test_f8_provider_unparseable_or_zero_override_falls_back_never_raises(bad):
    assert _provider_in_ctx({"auth.resend_verification": bad}) == "3/hour"


def test_format_limit_output_is_always_parseable_by_limits():
    from limits import parse_many

    for period in (1, 45, 60, 90, 3600, 7200, 86400, 172800):
        (item,) = parse_many(format_limit(7, period))
        assert item.amount == 7
        assert item.get_expiry() == period
