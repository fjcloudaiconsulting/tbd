import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ForecastPlanItemCreate(BaseModel):
    category_id: int
    type: Literal["income", "expense"]
    planned_amount: Decimal = Field(gt=0)
    source: Literal["manual", "recurring", "history"] = "manual"


class ForecastPlanItemUpdate(BaseModel):
    planned_amount: Decimal = Field(gt=0)


class ForecastPlanItemResponse(BaseModel):
    id: int
    plan_id: int
    category_id: int
    category_name: str = ""
    parent_id: Optional[int] = None
    type: Literal["income", "expense"]
    planned_amount: Decimal
    source: Literal["manual", "recurring", "history"]
    actual_amount: Decimal = Decimal("0.00")
    variance: Decimal = Decimal("0.00")

    model_config = {"from_attributes": True}


class ForecastPlanResponse(BaseModel):
    id: int
    billing_period_id: int
    period_start: datetime.date
    period_end: Optional[datetime.date] = None
    status: Literal["draft", "active"]
    total_planned_income: Decimal = Decimal("0.00")
    total_planned_expense: Decimal = Decimal("0.00")
    total_actual_income: Decimal = Decimal("0.00")
    total_actual_expense: Decimal = Decimal("0.00")
    items: list[ForecastPlanItemResponse] = []

    model_config = {"from_attributes": True}


class BulkUpsertItem(BaseModel):
    category_id: int
    type: Literal["income", "expense"]
    planned_amount: Decimal = Field(gt=0)
    source: Literal["manual", "recurring", "history"] = "manual"


class BulkUpsertRequest(BaseModel):
    items: list[BulkUpsertItem]


class CopyPlanRequest(BaseModel):
    source_period_start: datetime.date
    target_period_start: Optional[datetime.date] = None
