"""BE-01: no request decimal is unbounded.

PyMySQL renders a ``Decimal`` bind as a plain-notation literal, one character
per unit of exponent, so a request decimal needs a digit bound. Money
fields use ``schemas.common.Money`` (the ``Numeric(12, 2)`` column's own
bound); compared or what-if decimals use ``BoundedDecimal``.
"""
from __future__ import annotations

import types
import typing
from decimal import Decimal

import pytest
from pydantic import BaseModel, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from app.schemas.budget import BudgetCreate, BudgetUpdate
from app.schemas.common import money_or_none
from app.schemas.forecast_plan import (
    BulkUpsertItem,
    ForecastPlanItemCreate,
    ForecastPlanItemUpdate,
)
from app.schemas.import_schemas import ImportConfirmRow
from app.schemas.recurring import RecurringCreate, RecurringUpdate
from app.schemas.subscription import PlanCreate, PlanUpdate
from tests.app_routes import effective_routes

HUGE = ["1E+100000", "1e1000000000", "1E-100000", "NaN", "Infinity", "-Infinity"]
OUT_OF_COLUMN = ["10000000000", "12.345", "0.001"]
ORDINARY = ["125.50", "0.01", "9999999999.99", "10.500", "1.2E+3", 125.5, 42]


def _confirm_row(amount):
    return {
        "row_number": 1, "date": "2026-04-02", "description": "Albert Heijn",
        "amount": amount, "type": "expense",
    }


# (model, payload builder, field name)
MONEY_FIELDS = [
    (BudgetCreate, lambda a: {"category_id": 1, "amount": a}, "amount"),
    (BudgetUpdate, lambda a: {"amount": a}, "amount"),
    (
        RecurringCreate,
        lambda a: {
            "account_id": 1, "category_id": 1, "description": "Rent", "amount": a,
            "type": "expense", "frequency": "monthly", "next_due_date": "2026-05-01",
        },
        "amount",
    ),
    (RecurringUpdate, lambda a: {"amount": a}, "amount"),
    (ForecastPlanItemCreate, lambda a: {"category_id": 1, "type": "expense", "planned_amount": a}, "planned_amount"),
    (ForecastPlanItemUpdate, lambda a: {"planned_amount": a}, "planned_amount"),
    (BulkUpsertItem, lambda a: {"category_id": 1, "type": "income", "planned_amount": a}, "planned_amount"),
    (ImportConfirmRow, _confirm_row, "amount"),
]
_IDS = [f"{m.__name__}.{f}" for m, _, f in MONEY_FIELDS]


@pytest.mark.parametrize("model,build,field", MONEY_FIELDS, ids=_IDS)
@pytest.mark.parametrize("value", HUGE + OUT_OF_COLUMN)
def test_money_field_rejects_unbounded_and_unstorable(model, build, field, value):
    with pytest.raises(PydanticValidationError) as exc:
        model.model_validate(build(value))
    assert any(e["loc"] == (field,) for e in exc.value.errors())


@pytest.mark.parametrize("model,build,field", MONEY_FIELDS, ids=_IDS)
@pytest.mark.parametrize("value", ORDINARY)
def test_money_field_still_accepts_ordinary_amounts(model, build, field, value):
    parsed = model.model_validate(build(value))
    assert getattr(parsed, field) == Decimal(str(value))


@pytest.mark.parametrize("model", [BudgetCreate, ForecastPlanItemCreate, ImportConfirmRow])
def test_money_field_keeps_its_gt_zero(model):
    build = {m: b for m, b, _ in MONEY_FIELDS}[model]
    with pytest.raises(PydanticValidationError):
        model.model_validate(build("0"))


@pytest.mark.parametrize("model", [PlanCreate, PlanUpdate])
def test_plan_prices_are_bounded_like_their_numeric_10_2_column(model):
    base = {"name": "Pro", "slug": "pro"} if model is PlanCreate else {}
    model.model_validate({**base, "price_monthly": "99999999.99", "price_yearly": "9.5"})
    for bad in HUGE + ["100000000", "9.999"]:
        with pytest.raises(PydanticValidationError):
            model.model_validate({**base, "price_monthly": bad})


@pytest.mark.parametrize("value", HUGE + OUT_OF_COLUMN)
def test_money_or_none_rejects(value):
    assert money_or_none(Decimal(value)) is None


def test_money_or_none_passes_storable_amounts_through():
    for value in ["12.30", "-42", "9999999999.99", "1.230"]:
        assert money_or_none(Decimal(value)) == Decimal(value)


# ── fence: every Decimal reachable from a request body is bounded ──────────


def _decimal_leaves(model: type[BaseModel], seen: set) -> list[tuple[str, TypeAdapter]]:
    """(dotted field path, adapter) for every Decimal field under ``model``,
    recursing through nested models in lists, optionals and unions."""
    if model in seen:
        return []
    seen.add(model)
    out = []
    for name, info in model.model_fields.items():
        nested, is_decimal = [], False
        stack = [info.annotation]
        while stack:
            tp = stack.pop()
            if tp is Decimal:
                is_decimal = True
            elif isinstance(tp, type) and issubclass(tp, BaseModel):
                nested.append(tp)
            elif typing.get_origin(tp) is typing.Annotated:
                stack.append(typing.get_args(tp)[0])
            elif typing.get_origin(tp) is not None or isinstance(tp, types.UnionType):
                stack.extend(typing.get_args(tp))
        if is_decimal:
            annotation = info.annotation
            if info.metadata:
                annotation = typing.Annotated[annotation, *info.metadata]
            adapter = TypeAdapter(annotation)
            out.append((f"{model.__name__}.{name}", adapter))
        for sub in nested:
            out.extend(_decimal_leaves(sub, seen))
    return out


def _request_body_models() -> set[type[BaseModel]]:
    from app.main import app

    models = set()
    for route in effective_routes(app):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        stack = list(dependant.body_params)
        stack += [p for d in dependant.dependencies for p in d.body_params]
        for param in stack:
            tp = param.field_info.annotation
            for t in (tp, *typing.get_args(tp)):
                if isinstance(t, type) and issubclass(t, BaseModel):
                    models.add(t)
    return models


def test_every_request_body_decimal_rejects_huge_exponents():
    """FENCE. Wrong implementation: a new request model declaring
    ``amount: Decimal = Field(gt=0)`` (the BE-01 shape) instead of ``Money``."""
    leaves, seen = [], set()
    for model in _request_body_models():
        leaves.extend(_decimal_leaves(model, seen))
    assert any(path == "BudgetCreate.amount" for path, _ in leaves), "fence found nothing"
    unbounded = []
    for path, adapter in leaves:
        for probe in ("1E+100000", "1E-100000"):
            try:
                adapter.validate_python(probe)
            except PydanticValidationError:
                continue
            unbounded.append((path, probe))
    assert unbounded == []
