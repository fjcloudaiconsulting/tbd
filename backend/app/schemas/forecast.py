"""Pydantic shapes for the forecast endpoints.

Both GET /api/v1/forecast (the period rollup) and
GET /api/v1/forecast/account-balances (the per-account projection) are
typed response models. Money amounts on the period rollup are carried as
strings because ``forecast_service.compute_forecast`` string-serialises
its Decimals; the models mirror that wire contract exactly.
"""

import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class AccountBalanceForecastTotal(BaseModel):
    currency: str
    balance: Decimal
    pending_delta: Decimal
    expected_month_end_balance: Decimal


class CcPaymentLine(BaseModel):
    """A synthesized credit-card payment on the per-account forecast line
    (provenance source="credit_card_payment"). ``amount`` is the projected
    outflow on ``date`` (the resolved cycle due date)."""

    amount: Decimal
    date: datetime.date


class AccountBalanceForecastRow(BaseModel):
    account_id: int
    account_name: str
    currency: str
    is_default: bool
    account_type_slug: Optional[str] = None
    balance: Decimal
    pending_delta: Decimal
    expected_month_end_balance: Decimal
    cc_payments: list[CcPaymentLine] = []


class AccountBalanceForecastResponse(BaseModel):
    period_start: datetime.date
    period_end: datetime.date
    totals: list[AccountBalanceForecastTotal]
    accounts: list[AccountBalanceForecastRow]


class ForecastCategoryRow(BaseModel):
    """Per-category executed + forecast breakdown for the period rollup.

    Amounts are strings (string-serialised Decimals) to match the wire
    contract emitted by ``forecast_service.compute_forecast``.
    """

    category_id: int
    category_name: str
    parent_id: Optional[int] = None
    executed: str
    pending: str
    recurring: str
    forecast: str


class ForecastResponse(BaseModel):
    """Full period forecast: settled + pending + upcoming recurring.

    Money fields are strings to preserve the exact wire shape produced by
    ``forecast_service.compute_forecast``. The period bounds are dates
    (serialised back to the same ISO strings the service emits).
    """

    period_start: datetime.date
    period_end: datetime.date
    executed_income: str
    executed_expense: str
    executed_net: str
    pending_income: str
    pending_expense: str
    recurring_income: str
    recurring_expense: str
    forecast_income: str
    forecast_expense: str
    forecast_net: str
    categories: list[ForecastCategoryRow]
