"""Shared schemas and field types reused across routers.

``ListEnvelope`` standardizes the shape every admin (and, going forward,
non-admin) list endpoint returns: ``{items, total, limit, offset}``. The
five admin tables (users, orgs, subscriptions, audit, rate-limit-overrides)
fan out from this single envelope so the frontend's shared ``useTableState``
+ ``Pagination`` components can read one contract everywhere.

``total`` is the COUNT over the *same filtered query* as ``items`` (minus
limit/offset), never the unfiltered table size. See ``app.services.list_query``
for the org-scoping contract that keeps ``total`` from leaking a cross-scope
count.

``Money`` is the request-side type for an amount stored in a
``Numeric(12, 2)`` column (see its comment).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Generic, Optional, TypeVar

from pydantic import BaseModel, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

T = TypeVar("T")


# BE-01. Every money column is ``Numeric(12, 2)``. A request ``Decimal``
# without a digit bound reaches the MySQL driver unchanged, and PyMySQL
# renders it as a plain-notation literal with one character per unit of
# exponent, so the statement grows with the exponent rather than with the
# request. ``Money`` mirrors the column instead: finite, at most 12 digits, 2 of
# them after the point (Pydantic ignores trailing zeros, so "10.500" passes).
# Sign and ``gt``/``ge`` stay at the use site.
MONEY_MAX_DIGITS = 12
MONEY_DECIMAL_PLACES = 2

Money = Annotated[
    Decimal,
    Field(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        allow_inf_nan=False,
    ),
]

# For request decimals that are compared or computed with, never stored in a
# money column: a report filter bound ("amount >= 12.345" is a fair question,
# and a JS float can carry 17 significant digits) or a plan's what-if inputs.
# Still finite and of bounded size (14 whole and 20 fractional digits, a
# literal under 40 characters), so no request decimal is unbounded.
BOUNDED_DECIMAL_MAX_DIGITS = 34
BOUNDED_DECIMAL_DECIMAL_PLACES = 20

BoundedDecimal = Annotated[
    Decimal,
    Field(
        max_digits=BOUNDED_DECIMAL_MAX_DIGITS,
        decimal_places=BOUNDED_DECIMAL_DECIMAL_PLACES,
        allow_inf_nan=False,
    ),
]

_MONEY_ADAPTER: TypeAdapter[Decimal] = TypeAdapter(Money)


def money_or_none(value: Decimal) -> Optional[Decimal]:
    """Return ``value`` if it fits ``Money``, else ``None``.

    For parsers that turn free text into an amount outside a request model
    (search box, import files) and must apply the same bound.
    """
    try:
        return _MONEY_ADAPTER.validate_python(value)
    except PydanticValidationError:
        return None


class ListEnvelope(BaseModel, Generic[T]):
    """Generic paginated list response.

    ``items`` is the current page; ``total`` is the full filtered row count
    (so the client can compute page count); ``limit``/``offset`` echo the
    clamped values the server actually applied.
    """

    items: list[T]
    total: int
    limit: int
    offset: int
