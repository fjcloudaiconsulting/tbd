"""Two routes that share a URL prefix and NOTHING else (TBD-197).

⚠⚠ **DO NOT MOVE THE FEATURE GATE ONTO THIS ROUTER.** ⚠⚠

``GET /api/v1/forecast`` is a Forecast feature and carries a HANDLER-level
``Feature.FORECAST`` dependency.

``GET /api/v1/forecast/account-balances`` is **deliberately UNGATED**. Despite
the URL it is not a Forecast feature at all: ``account_balance_forecast_service``
imports no ``ForecastPlan`` and no ``Budget``. It is an account-projection
engine — credit-card statement cycles, loan amortization, recurring templates —
and its consumers are ``LoanPayoffTile`` and ``CreditUtilizationWidget``, which
are Credit-Card and Loan surfaces that an org switching Forecast off never
asked to lose. It lives under this prefix by mounting accident, not by kinship.

The inconsistency is real, and leaving it is the decision. An engineer "tidying"
it by lifting the dependency to ``APIRouter(...)`` closes account-balances, and
the failure is SILENT: the two tiles lose their data source and nothing else in
the suite notices. Fence F7 in ``tests/test_forecast_toggle.py`` exists solely
to notice — it asserts **200 on account-balances and 404 on /forecast for the
same org**, in one test.

Recorded here because it makes over-gating worse than merely wrong:
``AccountMonthEndForecast.tsx`` renders a bare "Loading…" forever on null data.
Closing this route yields a permanent false loading state, not an empty state.

Do not apply service provenance mechanically in the other direction either:
``forecast_service`` imports no ``ForecastPlan`` yet ``GET /api/v1/forecast``
IS gated. The distinction is consumer-side — its one consumer, ``OnTrackWidget``,
compares the projection against the plan from ``/forecast-plans/current``.
"""
import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.forecast import AccountBalanceForecastResponse, ForecastResponse
from app.services import account_balance_forecast_service, forecast_service
from app.services.feature_gate import Feature
from app.services.feature_gate import require_feature as require_product_area

# No router-level ``dependencies=[...]`` here, ON PURPOSE — see above. The gate
# is attached to ``get_forecast`` and to nothing else in this module.
router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])


@router.get(
    "",
    response_model=ForecastResponse,
    dependencies=[Depends(require_product_area(Feature.FORECAST))],
)
async def get_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    return await forecast_service.compute_forecast(
        db, current_user.org_id, period_start=period_start
    )


@router.get("/account-balances", response_model=AccountBalanceForecastResponse)
async def get_account_balance_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    period_start: datetime.date | None = Query(default=None),
):
    """Per-account expected month-end balance for a billing period.

    ⚠ UNGATED by design (TBD-197) — read the module docstring before adding a
    dependency here or on the router.

    Dashboard-only view: balance + pending delta in the period. Excludes
    settled rows (already in stored balance) and manual adjustments
    (settled-only today). Includes pending transfer legs because they
    move per-account balances even though they aren't reportable.
    """
    return await account_balance_forecast_service.compute_account_balance_forecast(
        db, current_user.org_id, period_start=period_start
    )
