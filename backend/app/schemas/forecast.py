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

from pydantic import BaseModel, ConfigDict, Field


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


class LoanPaymentLine(BaseModel):
    """A synthesized loan payment on the per-account forecast line (Loan V1
    Slice 2). ``amount`` is the projected outflow (capped at the outstanding
    balance) on ``date`` (the scheduled payment date)."""

    amount: Decimal
    date: datetime.date


class RecurringLine(BaseModel):
    """One PROJECTED (not yet materialised) recurring occurrence on the
    per-account forecast line (TBD-198).

    ``amount`` is SIGNED — income positive, expense negative — unlike
    ``CcPaymentLine`` / ``LoanPaymentLine``, which are always outflows and
    carry a magnitude. ``date`` is the occurrence's own due date; when that
    date is behind the clock the delta is booked on ``series_start`` instead
    (see ``_add_day_delta``).
    """

    amount: Decimal
    date: datetime.date


class DailyBalancePoint(BaseModel):
    """One END-OF-DAY projected balance (TBD-198).

    Emitted on the wire deliberately, not kept private to the service: it is
    what makes ``daily_balances[-1].balance == expected_month_end_balance``
    assertable at the API boundary rather than only inside a unit test, and it
    is the line-item visibility docs/product/PRODUCT.md asks for — the user can see WHICH
    day the money runs out, not merely that it does.
    """

    date: datetime.date
    balance: Decimal


class RiskDayRun(BaseModel):
    """One contiguous below-zero interval on an account's daily series.

    A RUN, never a day (R2): ``[from .. through]`` inclusive, with the trough
    and the date it lands on. ``from`` is a Python keyword, so the field is
    ``from_date`` with a wire alias; FastAPI serialises response models with
    ``by_alias=True``, so the JSON key is ``from``.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_date: datetime.date = Field(alias="from")
    through: datetime.date
    lowest_balance: Decimal
    lowest_on: datetime.date


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
    loan_payments: list[LoanPaymentLine] = []
    recurring_lines: list[RecurringLine] = []
    # TBD-198. Both live on the ROW, never on AccountBalanceForecastTotal:
    # each account has exactly one currency, so a per-account series never
    # sums anything, and `totals` is the one place a cross-currency sum could
    # enter. Do not "roll up" either field.
    daily_balances: list[DailyBalancePoint] = []
    risk_days: list[RiskDayRun] = []


class AccountBalanceForecastResponse(BaseModel):
    period_start: datetime.date
    # TBD-198. The first day of `daily_balances`, i.e. `max(period_start,
    # today)` clamped to `period_end`. NOT redundant with `period_start`: every
    # delta dated before it is FLOORED onto it, so it is the only thing that
    # tells a client whether day 0 is one day's activity or a pile of re-booked
    # overdue obligations.
    series_start: datetime.date
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
