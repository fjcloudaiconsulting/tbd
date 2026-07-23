"""Pydantic schemas for the ``/api/v1/dashboard`` endpoints.

Models the per-org saved dashboard layout (layout JSON, canvas filter JSON,
schema version).

Architecture notes:

- ``layout_json`` validation uses a dashboard-specific validator
  (``validate_dashboard_layout_json``) defined in this module.  It accepts
  both the dashboard-native ``dash_*`` widget types AND all report widget
  types (kpi, bar, line, …) so the dashboard can host cloned report widgets
  alongside finance tiles.

  The reports validator (``validate_layout_json`` in ``report_layout``) is
  intentionally NOT used for the dashboard: it uses a closed ``WidgetType``
  enum that does NOT include the ``dash_*`` discriminants, so it would 422
  every layout that contains a finance tile.  The reports validator stays
  strict (unchanged) — it never sees ``dash_*`` payloads.

- ``canvas_filters_json`` reuses the shared ``validate_canvas_filters_json``
  from ``report_layout`` — the canvas-filter shape is identical on both
  surfaces.

- Validators use the validate-and-return-verbatim pattern (side-effect only,
  no ``model_dump`` round-trip). This prevents the #424 regression where
  ``extra="ignore"`` widget configs silently strip unmodelled visual knobs
  (``compare_prior_period``, ``top_n``, ``smooth``, ``stacked``, etc.).

- ``DashboardUpdate`` uses ``extra="forbid"`` so unknown keys are rejected
  (matches ``ReportUpdate`` behaviour).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.report_layout import (
    AreaWidget,
    BarWidget,
    KPIWidget,
    LineWidget,
    PieWidget,
    SankeyWidget,
    SparklineWidget,
    StackedBarWidget,
    TableWidget,
    WidgetGrid,
    validate_canvas_filters_json,
)


# ─── dashboard-native widget types (dash_*) ───────────────────────────────────


class DashWidgetType(str, enum.Enum):
    ON_TRACK = "dash_on_track"
    ACCOUNTS = "dash_accounts"
    ACCOUNT_FORECAST = "dash_account_forecast"
    SPENDING = "dash_spending"
    BUDGET = "dash_budget"
    FORECAST_CATEGORY = "dash_forecast_category"
    RECENT_TRANSACTIONS = "dash_recent_transactions"
    CC_UTILIZATION = "dash_cc_utilization"


class _DashWidgetConfig(BaseModel):
    """Config for dashboard-native tiles.

    The provider supplies all data; config is always an empty object.
    ``extra="ignore"`` silently drops unknown keys on the way in while
    still preserving the verbatim-return contract (the validator returns
    the original dict, not a model_dump round-trip).  This matches the
    ``extra="ignore"`` policy used by the report widget configs
    (``_SingleMeasureConfig`` / ``_MultiSeriesConfig``) that share the
    same discriminated union, keeping the policy consistent across all
    widget config models.
    """

    model_config = ConfigDict(extra="ignore")


class _DashWidgetBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    grid: WidgetGrid


class DashOnTrackWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.ON_TRACK]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashAccountsWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.ACCOUNTS]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashAccountForecastWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.ACCOUNT_FORECAST]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashSpendingWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.SPENDING]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashBudgetWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.BUDGET]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashForecastCategoryWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.FORECAST_CATEGORY]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashRecentTransactionsWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.RECENT_TRANSACTIONS]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


class DashCcUtilizationWidget(_DashWidgetBase):
    type: Literal[DashWidgetType.CC_UTILIZATION]
    config: _DashWidgetConfig = Field(default_factory=_DashWidgetConfig)


# ─── widened widget union (dash_* + all report types) ────────────────────────
#
# Re-uses the public report widget classes from report_layout.  The
# discriminator field lets Pydantic route each incoming dict to the right
# model without ambiguity.
_DashboardWidget = Annotated[
    Union[
        DashOnTrackWidget,
        DashAccountsWidget,
        DashAccountForecastWidget,
        DashSpendingWidget,
        DashBudgetWidget,
        DashForecastCategoryWidget,
        DashRecentTransactionsWidget,
        DashCcUtilizationWidget,
        # report widget types (public classes from report_layout)
        KPIWidget,
        BarWidget,
        PieWidget,
        SparklineWidget,
        LineWidget,
        AreaWidget,
        StackedBarWidget,
        TableWidget,
        SankeyWidget,
    ],
    Field(discriminator="type"),
]


# ─── dashboard layout root ────────────────────────────────────────────────────


class _DashboardLayoutJson(BaseModel):
    """The ``layout_json`` shape accepted by the dashboard endpoints.

    Accepts ``dash_*`` widget types and all report widget types.
    Widget ``id`` values must be unique within the layout.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    widgets: List[_DashboardWidget]

    @field_validator("widgets")
    @classmethod
    def _unique_ids(
        cls, widgets: List[_DashboardWidget]
    ) -> List[_DashboardWidget]:
        ids = [w.id for w in widgets]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate widget id(s): {', '.join(dupes)}")
        return widgets


# ─── validation entrypoints ───────────────────────────────────────────────────


def validate_dashboard_layout_json(value: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``layout_json`` payload for the dashboard endpoint.

    Empty dict (a blank layout) passes through untouched. A populated dict is
    validated against ``_DashboardLayoutJson`` (accepts ``dash_*`` + all
    report types) and then the ORIGINAL dict is returned VERBATIM.

    We validate for the side-effect only — never round-trip through
    ``model_dump`` (the widget configs use ``extra="ignore"`` and dumping
    would silently DROP unmodelled-but-real visual knobs).

    The strict ``validate_layout_json`` from ``report_layout`` is NOT used
    here; it stays unchanged for the reports surface only.
    """
    if not isinstance(value, dict):
        raise ValueError("layout_json must be a JSON object")
    if value == {}:
        return value
    _DashboardLayoutJson.model_validate(value)
    return value


# ─── response / update schemas ────────────────────────────────────────────────


class DashboardLayoutOut(BaseModel):
    """Full dashboard layout response returned by GET/PATCH ``/api/v1/dashboard``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_user_id: int
    org_id: int
    layout_json: dict[str, Any]
    canvas_filters_json: dict[str, Any]
    schema_version: int
    created_at: datetime
    updated_at: datetime


class DashboardUpdate(BaseModel):
    """Partial update for the dashboard layout.

    Both fields are optional; absent keys leave the DB column unchanged.
    ``extra="forbid"`` rejects unknown keys to prevent silent misuse.
    """

    model_config = ConfigDict(extra="forbid")

    layout_json: Optional[dict[str, Any]] = None
    canvas_filters_json: Optional[dict[str, Any]] = None

    @field_validator("layout_json")
    @classmethod
    def _check_layout(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        # ``None`` (absent key) is left for the router's explicit-null guard;
        # only a present dict is validated here.
        if v is None:
            return v
        return validate_dashboard_layout_json(v)

    @field_validator("canvas_filters_json")
    @classmethod
    def _check_canvas_filters(
        cls, v: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if v is None:
            return v
        return validate_canvas_filters_json(v)
