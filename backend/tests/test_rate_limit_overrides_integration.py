"""Integration tests: ``dynamic_limit`` bridge to the override
resolver.

The bridge has two halves:

1. ``parse_default_limit`` / ``format_limit`` round-trip parser.
2. The runtime callable returned by ``dynamic_limit(endpoint, default)``,
   which consults the resolver (with the DB + cache) and returns the
   per-request limit string.

This file pins the parser shape (cheap; pure function) and an
import-time-error assertion (an unparseable default crashes early,
not at first request). The full request-path integration that
demonstrates a tighter override hitting 429 before the global default
is covered by the router test for the create endpoint plus the
service test for the resolver; wiring both into a live limiter inside
an in-memory sqlite app would require a running Redis (the limiter's
storage backend) and is left for a manual verify pass.
"""
from __future__ import annotations

import pytest

from app.rate_limit_overrides import (
    dynamic_limit,
    format_limit,
    parse_default_limit,
)


def test_parse_default_limit_word_forms():
    assert parse_default_limit("20/minute") == (20, 60)
    assert parse_default_limit("5/hour") == (5, 3600)
    assert parse_default_limit("3/day") == (3, 86400)
    assert parse_default_limit("100/second") == (100, 1)
    # Whitespace tolerated.
    assert parse_default_limit("  10 / minute  ") == (10, 60)
    # Case tolerated.
    assert parse_default_limit("10/Minute") == (10, 60)


def test_parse_default_limit_numeric_period():
    assert parse_default_limit("30/45") == (30, 45)


def test_parse_default_limit_rejects_bogus():
    with pytest.raises(ValueError):
        parse_default_limit("nonsense")
    with pytest.raises(ValueError):
        parse_default_limit("20/forever")
    with pytest.raises(ValueError):
        parse_default_limit("20/")


def test_format_round_trip():
    assert parse_default_limit(format_limit(42, 60)) == (42, 60)
    assert parse_default_limit(format_limit(5, 3600)) == (5, 3600)


def test_dynamic_limit_validates_default_at_construction():
    """An unparseable default raises immediately so a typo in a
    decorator argument crashes import, not the first request.
    """
    with pytest.raises(ValueError):
        dynamic_limit("auth.login", "20/forever")


def test_dynamic_limit_returns_callable_that_falls_through_to_default():
    """No request identity available -> resolver returns ``None`` ->
    callable returns the default.
    """
    fn = dynamic_limit("auth.login", "20/minute")

    class _FakeReq:
        class state:
            pass

        headers: dict = {}

    # The fake request has no auth header and no state user/org; the
    # resolver path short-circuits to the default.
    assert fn(_FakeReq()) == "20/minute"
