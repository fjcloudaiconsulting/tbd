"""TBD-471 — ``FilterField.TRANSFER`` scalar coercion.

⚠ WHY THIS EXISTS. The compiler's ``_asks_for_transfers`` keys on
``f.value is True`` -- an identity check against the singleton. If coercion ever
regressed to returning the STRING ``"true"``, the predicate would be False, the
reportability base would silently not stand down, and ``transfer=true`` would
compile to ``linked_transaction_id IS NULL AND EXISTS(reciprocal)`` -- an
unsatisfiable conjunction returning zero rows with no error. That is precisely
the defect class TBD-471 exists to remove, so the string arms of the coercion
are load-bearing and were otherwise exercised by nothing: every other test in
the suite passes a real ``bool``.
"""
from __future__ import annotations

import pytest

from app.schemas.reports_query import Filter, FilterField, FilterOp


@pytest.mark.parametrize(
    "raw, expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("  TRUE  ", True),
    ],
)
def test_transfer_coerces_to_a_real_bool(raw, expected):
    f = Filter(field=FilterField.TRANSFER, op=FilterOp.EQ, value=raw)
    assert f.value is expected, (
        f"{raw!r} coerced to {f.value!r}; ``_asks_for_transfers`` tests "
        f"``is True``, so anything but the bool singleton silently disables "
        f"the reportability stand-down"
    )


@pytest.mark.parametrize("raw", ["yes", "on", "", "maybe", "2"])
def test_transfer_rejects_non_boolean_values(raw):
    with pytest.raises(ValueError):
        Filter(field=FilterField.TRANSFER, op=FilterOp.EQ, value=raw)
