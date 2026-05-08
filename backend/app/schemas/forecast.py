"""Pydantic shapes for the account-balance forecast endpoint.

The legacy forecast aggregate (GET /api/v1/forecast) returns an untyped
dict and stays that way for now — it's a deeply nested rollup with a
churning shape. The newer per-account projection has a stable contract,
so we type it.
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


class AccountBalanceForecastRow(BaseModel):
    account_id: int
    account_name: str
    currency: str
    is_default: bool
    account_type_slug: Optional[str] = None
    balance: Decimal
    pending_delta: Decimal
    expected_month_end_balance: Decimal


class AccountBalanceForecastResponse(BaseModel):
    period_start: datetime.date
    period_end: datetime.date
    totals: list[AccountBalanceForecastTotal]
    accounts: list[AccountBalanceForecastRow]
