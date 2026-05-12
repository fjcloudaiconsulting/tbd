"""Unit tests for ``normalize_email`` — the canonical email shape
applied at every ``users``-row create site and email lookup.
"""
from __future__ import annotations

import pytest

from app.services.user_service import normalize_email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice@example.com", "alice@example.com"),
        ("Alice@Example.com", "alice@example.com"),
        ("ALICE@EXAMPLE.COM", "alice@example.com"),
        ("  alice@example.com  ", "alice@example.com"),
        ("\talice@example.com\n", "alice@example.com"),
        ("Alice+Filter@Example.COM", "alice+filter@example.com"),
    ],
)
def test_normalize_email_canonical(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


def test_normalize_email_is_idempotent() -> None:
    """Re-running normalize_email on a normalized value is a no-op.

    Important for sites that compose lookups (``normalize_email`` on
    the way in and again on the way out via a helper).
    """
    first = normalize_email("  USER@EXAMPLE.com  ")
    assert normalize_email(first) == first
