"""Reports v2 — query AST schemas.

Closed Pydantic surface for the ``POST /api/v1/reports/query`` endpoint.
The architect-locked rule (spec §6 "Security model"): the AST cannot
describe SQL. Every dimension, aggregation, and filter field is drawn
from a closed enum / literal. Anything outside the whitelist returns
422 before any DB work happens.

Hard caps enforced at the schema layer (spec §6 "Hard caps"):

- ``limit`` between 1 and 500.
- ``filters`` list at most 20 entries.
- ``dimensions`` list at most 2 entries.
- Date BETWEEN window at most 5 years.

The schema deliberately does NOT accept ``org_id`` / ``user_id`` /
``organization_id`` anywhere — those columns are not in the filter
whitelist and the AST has no way to describe them. The router injects
``org_id`` from ``get_current_user()`` server-side. ``extra="forbid"``
on every model rejects unknown keys at the wire.
"""
from __future__ import annotations

import enum
from datetime import date
from decimal import Decimal
from typing import Any, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    conint,
    field_validator,
    model_validator,
)


# Hard caps. Module-level constants so tests can import + assert them.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
MAX_FILTERS = 20
MAX_DIMENSIONS = 2
MAX_DATE_WINDOW_DAYS = 5 * 365 + 2  # 5 years inclusive of two leap days.


class Dataset(str, enum.Enum):
    """Closed source set. v1 only exposes ``transactions``; opening
    new datasets is a v2 spec decision (budgets, recurring, forecast).
    Admin / internal tables are deliberately excluded — adding one
    here is a security review event.
    """

    TRANSACTIONS = "transactions"


class Aggregation(str, enum.Enum):
    """Closed aggregation set. Anything outside this enum is rejected
    by Pydantic at schema-validation time, so the compiler never sees
    user-supplied aggregation strings.
    """

    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    # ``distinct`` is shorthand for COUNT(DISTINCT field) — the
    # compiler expands it. Spec section 3 lists ``count_distinct``
    # as the long form; we accept the short form on the wire for
    # symmetry with the other aggs.
    DISTINCT = "distinct"


class MeasureField(str, enum.Enum):
    """Columns an aggregation may target on the transactions source.
    Closed enum so a misspelled field cannot reach SQL.
    """

    AMOUNT = "amount"
    ID = "id"
    CATEGORY_ID = "category_id"
    ACCOUNT_ID = "account_id"


class Dimension(str, enum.Enum):
    """GROUP BY dimensions. Closed enum.

    Time bucket dimensions (``month``, ``week``, ``day``) compile to
    SQLAlchemy ``func.date_format`` expressions in MySQL and SQLite-
    compatible ``strftime`` equivalents in the test harness.
    """

    CATEGORY = "category"
    CATEGORY_MASTER = "category_master"
    ACCOUNT = "account"
    TAG = "tag"
    TXN_TYPE = "txn_type"
    STATUS = "status"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"


class FilterField(str, enum.Enum):
    """Filter columns. Closed enum — ``org_id`` / ``user_id`` /
    ``organization_id`` ARE NOT HERE on purpose. The router injects
    ``org_id`` from the authenticated context; nothing on the wire can
    describe an org boundary.
    """

    DATE = "date"
    AMOUNT = "amount"
    CATEGORY_ID = "category_id"
    ACCOUNT_ID = "account_id"
    TXN_TYPE = "txn_type"
    STATUS = "status"
    # Tag filter uses normalized tag name (lowercased), matching the
    # transactions list semantics at
    # ``backend/app/routers/transactions.py:90`` and
    # ``backend/app/services/transaction_service.py:1697``.
    TAG_NAME = "tag_name"


class FilterOp(str, enum.Enum):
    EQ = "eq"
    IN = "in"
    # ``between`` is allowed on date + amount only.
    BETWEEN = "between"
    GTE = "gte"
    LTE = "lte"


class Measure(BaseModel):
    """A single aggregation: ``agg(field)``.

    Validation is intentionally narrow: COUNT is the only agg that
    makes sense without a numeric column (``count(id)`` ≈ row count);
    SUM / AVG demand a numeric column (``amount``); DISTINCT pairs
    with a column the AST allows. Anything else returns 422.
    """

    model_config = ConfigDict(extra="forbid")

    agg: Aggregation
    field: MeasureField

    @model_validator(mode="after")
    def _validate_agg_field(self):
        # SUM / AVG require a numeric column. ``amount`` is the only
        # numeric column we expose on transactions.
        if self.agg in (Aggregation.SUM, Aggregation.AVG):
            if self.field is not MeasureField.AMOUNT:
                raise ValueError(
                    f"agg={self.agg.value} requires field='amount'; "
                    f"got field={self.field.value!r}"
                )
        # COUNT and DISTINCT accept any whitelisted field.
        return self


class TagMatch(str, enum.Enum):
    """Tag filter semantics — mirrors the transactions list contract
    at ``backend/app/routers/transactions.py:90``.

    - ``all`` (default): the row must carry EVERY named tag (AND).
    - ``any``: the row must carry AT LEAST ONE of the named tags (OR).
    """

    ALL = "all"
    ANY = "any"


class Filter(BaseModel):
    """A single AST filter primitive.

    ``value`` is narrowed by ``(field, op)`` in the model validator.
    BETWEEN demands a 2-element list (start, end) for date / amount
    only. IN demands a list. EQ / GTE / LTE demand a scalar.

    Tag filters carry an optional ``tag_match`` (default ``all``).
    """

    model_config = ConfigDict(extra="forbid")

    field: FilterField
    op: FilterOp
    value: Any
    # Tag-only knob. Default ``all`` matches the transactions list
    # contract. Ignored (silently allowed) on non-tag filters because
    # the compiler reads it conditionally.
    tag_match: TagMatch = TagMatch.ALL

    @model_validator(mode="after")
    def _validate_value(self):
        f, op = self.field, self.op

        # Type narrowing. The schema layer's job is to reject anything
        # the compiler can't safely handle.
        if op is FilterOp.BETWEEN:
            if f not in (FilterField.DATE, FilterField.AMOUNT):
                raise ValueError(
                    f"op='between' only valid on date / amount; got field={f.value!r}"
                )
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError(
                    "op='between' requires a 2-element [start, end] list"
                )
            if f is FilterField.DATE:
                start, end = self.value
                if not isinstance(start, (date, str)) or not isinstance(end, (date, str)):
                    raise ValueError(
                        "date BETWEEN bounds must be ISO dates"
                    )
                # Normalize to date.
                self.value = [_coerce_date(start), _coerce_date(end)]
                d_start, d_end = self.value
                if d_start > d_end:
                    raise ValueError("date BETWEEN start must be <= end")
                if (d_end - d_start).days > MAX_DATE_WINDOW_DAYS:
                    raise ValueError(
                        f"date BETWEEN window must be <= {MAX_DATE_WINDOW_DAYS} days "
                        "(5 years)"
                    )
            else:  # amount
                lo, hi = self.value
                if not isinstance(lo, (int, float, str, Decimal)) or not isinstance(
                    hi, (int, float, str, Decimal)
                ):
                    raise ValueError(
                        "amount BETWEEN bounds must be numeric"
                    )
                self.value = [_coerce_decimal(lo), _coerce_decimal(hi)]

        elif op is FilterOp.IN:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("op='in' requires a non-empty list")
            self.value = [_coerce_filter_scalar(f, v) for v in self.value]

        elif op in (FilterOp.EQ, FilterOp.GTE, FilterOp.LTE):
            if isinstance(self.value, list):
                raise ValueError(
                    f"op={op.value!r} requires a scalar value, not a list"
                )
            self.value = _coerce_filter_scalar(f, self.value)

        return self


class SortDir(str, enum.Enum):
    ASC = "asc"
    DESC = "desc"


class SortBy(str, enum.Enum):
    """The compiler can sort by the aggregated measure ('value') or
    by a dimension column. Anything else is rejected.
    """

    VALUE = "value"
    DIMENSION = "dimension"


class SortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: SortBy = SortBy.VALUE
    dir: SortDir = SortDir.DESC


class ReportsQuery(BaseModel):
    """The AST root.

    ``extra="forbid"`` is the load-bearing piece: a payload with an
    ``org_id`` (or any other non-whitelisted key) is rejected 422.
    The router injects ``org_id`` from the auth context.
    """

    model_config = ConfigDict(extra="forbid")

    dataset: Dataset
    measure: Measure
    dimensions: List[Dimension] = Field(default_factory=list, max_length=MAX_DIMENSIONS)
    filters: List[Filter] = Field(default_factory=list, max_length=MAX_FILTERS)
    sort: Optional[SortSpec] = None
    limit: conint(ge=1, le=MAX_LIMIT) = DEFAULT_LIMIT


class QueryMeta(BaseModel):
    row_count: int
    truncated: bool
    query_ms: int


class QueryRow(BaseModel):
    """Heterogeneous row dict — keys are dimension names plus ``value``."""

    model_config = ConfigDict(extra="allow")


class ReportsQueryResponse(BaseModel):
    rows: List[dict]
    meta: QueryMeta


# ─── helpers ────────────────────────────────────────────────────────


def _coerce_date(value):
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid ISO date: {value!r}") from exc
    raise ValueError(f"expected date or ISO string, got {type(value).__name__}")


def _coerce_decimal(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc


def _coerce_filter_scalar(field: FilterField, value):
    if field is FilterField.DATE:
        return _coerce_date(value)
    if field is FilterField.AMOUNT:
        return _coerce_decimal(value)
    if field in (FilterField.CATEGORY_ID, FilterField.ACCOUNT_ID):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.value} must be an integer") from exc
    if field is FilterField.TXN_TYPE:
        v = str(value)
        if v not in ("income", "expense", "transfer"):
            raise ValueError(f"txn_type must be one of income / expense / transfer; got {v!r}")
        return v
    if field is FilterField.STATUS:
        v = str(value)
        if v not in ("settled", "pending"):
            raise ValueError(f"status must be one of settled / pending; got {v!r}")
        return v
    if field is FilterField.TAG_NAME:
        v = str(value).strip().lower()
        if not v:
            raise ValueError("tag_name must be a non-empty string")
        return v
    # Should be unreachable given the FilterField enum is closed.
    raise ValueError(f"unsupported filter field {field!r}")
