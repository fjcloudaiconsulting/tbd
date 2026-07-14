"""Reports v2 — strict ``layout_json`` / ``canvas_filters_json`` schema.

PR1 stored these two columns as lenient passthrough dicts. This module
(PR2) lands the strict layout validator wired into ``ReportCreate`` /
``ReportUpdate``.

The shapes here mirror the *implemented* frontend types in
``frontend/lib/reports/types.ts`` exactly, because the canvas renders the
stored dicts directly. A shape mismatch renders a blank widget, so the
write-side validator is the contract that keeps the two in sync.

Design rules (kept deliberately narrow but not over-tight):

- **Empty is valid.** A brand-new / blank report saves ``layout_json={}``
  and ``canvas_filters_json={}`` (the frontend's ``Record<string, never>``
  case). Both empty-dict forms pass straight through; we only validate
  the *populated* shape.
- **Closed enums where the frontend is closed** — ``type``, ``dataset``,
  ``agg``, ``field``, ``dimension``, ``format``, ``sort``. Anything
  outside these unions is a wire-contract violation → 422.
- **Single-measure vs multi-series split.** ``kpi`` / ``bar`` / ``pie`` /
  ``sparkline`` carry ``config.measure`` (a single ``Measure``); ``line``
  / ``area`` / ``stacked_bar`` / ``table`` carry ``config.measures`` (a
  non-empty list of ``SeriesConfig``). The widget ``type`` selects the
  config model via a discriminated union.
- **Lenient on widget config extras.** Each widget config allows extra
  keys (``extra="ignore"``) so a new optional visual knob added on the
  frontend (e.g. ``smooth`` / ``stacked`` / ``top_n``) does not 422 an
  otherwise-valid layout, and so pre-existing saved reports are never
  gratuitously rejected. The *envelope* (``version`` / ``widgets`` /
  widget ``id`` / ``type`` / ``title`` / ``grid``) is strict; the
  per-widget config interior is validated for the fields we model and
  tolerant of the rest.
"""
from __future__ import annotations

import enum
from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# Dataset / Aggregation / MeasureField / Dimension are the shared closed
# enum atoms — defined once in ``reports_enums`` so the saved-layout shape
# and the live ``/query`` AST (``reports_query``) cannot drift.
from app.schemas.reports_enums import (
    Aggregation,
    Dataset,
    Dimension,
    MeasureField,
    TxnStatus,
)


# ─── closed enums (mirror frontend unions) ──────────────────────────


class WidgetType(str, enum.Enum):
    KPI = "kpi"
    BAR = "bar"
    STACKED_BAR = "stacked_bar"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    SPARKLINE = "sparkline"
    TABLE = "table"
    SANKEY = "sankey"


class WidgetFormat(str, enum.Enum):
    CURRENCY = "currency"
    NUMBER = "number"
    PERCENT = "percent"


class SortBy(str, enum.Enum):
    VALUE = "value"
    DIMENSION = "dimension"


class SortDir(str, enum.Enum):
    ASC = "asc"
    DESC = "desc"


# ─── shared leaf models ─────────────────────────────────────────────


class Measure(BaseModel):
    """A single aggregation ``agg(field)`` — mirrors the AST ``Measure``."""

    model_config = ConfigDict(extra="forbid")

    agg: Aggregation
    field: MeasureField


class SeriesConfig(BaseModel):
    """One series entry on a multi-series widget."""

    model_config = ConfigDict(extra="forbid")

    measure: Measure
    label: Optional[str] = None


class WidgetSort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: SortBy
    dir: SortDir


class WidgetGrid(BaseModel):
    """Grid placement. All four cells are required, non-negative ints;
    width / height must be positive (a zero-size widget is malformed).
    """

    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


# ─── per-widget config models ───────────────────────────────────────
#
# ``extra="ignore"`` on every config: the envelope is strict, the config
# interior tolerates optional/visual extras the frontend may add (smooth,
# stacked, top_n, compare_prior_period, …) and any not-yet-modelled knob,
# so a forward-compatible layout never 422s and existing saved reports are
# not rejected.


class _SingleMeasureConfig(BaseModel):
    """Config for single-measure widgets (kpi, bar, pie, sparkline)."""

    model_config = ConfigDict(extra="ignore")

    dataset: Dataset
    measure: Measure
    dimensions: Optional[List[Dimension]] = None
    filters: Optional[dict[str, Any]] = None
    sort: Optional[WidgetSort] = None
    limit: Optional[int] = Field(default=None, ge=1)
    format: Optional[WidgetFormat] = None


class _MultiSeriesConfig(BaseModel):
    """Config for multi-series widgets (line, area, stacked_bar, table)."""

    model_config = ConfigDict(extra="ignore")

    dataset: Dataset
    measures: List[SeriesConfig] = Field(min_length=1)
    dimensions: Optional[List[Dimension]] = None
    filters: Optional[dict[str, Any]] = None
    sort: Optional[WidgetSort] = None
    limit: Optional[int] = Field(default=None, ge=1)
    format: Optional[WidgetFormat] = None


class _SankeyConfig(BaseModel):
    """Config for the sankey (cash-flow) widget.

    The query travels via the dedicated ``POST /api/v1/reports/query/sankey``
    endpoint (transactions + sum(amount) implied), but the layout still
    persists ``dataset`` + ``measure`` for editor uniformity, plus the two
    sankey-specific knobs (``spending_granularity`` and ``top_n``).
    ``extra="ignore"`` matches the other config models so forward-compat
    knobs never 422 and are preserved verbatim by validate_layout_json.
    """

    model_config = ConfigDict(extra="ignore")

    dataset: Dataset
    measure: Measure
    filters: Optional[dict[str, Any]] = None
    spending_granularity: Optional[
        Literal["category", "category_master"]
    ] = None
    top_n: Optional[int] = Field(default=None, ge=1)


# ─── widget envelope (discriminated by ``type``) ────────────────────


class _WidgetBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str
    grid: WidgetGrid


class KPIWidget(_WidgetBase):
    type: Literal[WidgetType.KPI]
    config: _SingleMeasureConfig


class BarWidget(_WidgetBase):
    type: Literal[WidgetType.BAR]
    config: _SingleMeasureConfig


class PieWidget(_WidgetBase):
    type: Literal[WidgetType.PIE]
    config: _SingleMeasureConfig


class SparklineWidget(_WidgetBase):
    type: Literal[WidgetType.SPARKLINE]
    config: _SingleMeasureConfig


class LineWidget(_WidgetBase):
    type: Literal[WidgetType.LINE]
    config: _MultiSeriesConfig


class AreaWidget(_WidgetBase):
    type: Literal[WidgetType.AREA]
    config: _MultiSeriesConfig


class StackedBarWidget(_WidgetBase):
    type: Literal[WidgetType.STACKED_BAR]
    config: _MultiSeriesConfig


class TableWidget(_WidgetBase):
    type: Literal[WidgetType.TABLE]
    config: _MultiSeriesConfig


class SankeyWidget(_WidgetBase):
    type: Literal[WidgetType.SANKEY]
    config: _SankeyConfig


Widget = Annotated[
    Union[
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


# ─── layout root + canvas filters ───────────────────────────────────


class LayoutJson(BaseModel):
    """The populated ``layout_json`` shape: ``{version: 1, widgets: [...]}``.

    Widget ``id`` values must be unique within the layout.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    widgets: List[Widget]

    @field_validator("widgets")
    @classmethod
    def _unique_ids(cls, widgets: List[Widget]) -> List[Widget]:
        ids = [w.id for w in widgets]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate widget id(s): {', '.join(dupes)}")
        return widgets


class CanvasDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Optional[str] = None
    end: Optional[str] = None


class CanvasFilters(BaseModel):
    """The populated ``canvas_filters_json`` shape."""

    model_config = ConfigDict(extra="forbid")

    date_range: Optional[CanvasDateRange] = None
    account_ids: Optional[List[int]] = None
    category_ids: Optional[List[int]] = None
    status: Optional[TxnStatus] = None


# ─── validation entrypoints (used by ReportCreate / ReportUpdate) ───


def validate_layout_json(value: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``layout_json`` payload.

    Empty dict (a blank / new report) passes through untouched. A
    populated dict is validated against ``LayoutJson`` (raises
    ``ValueError`` on any malformed shape; the Pydantic field validator
    that calls this surfaces it as a 422) and then the ORIGINAL dict is
    returned verbatim. We validate for the side-effect only — we must NOT
    round-trip through ``model_dump``, because the widget configs use
    ``extra="ignore"`` and dumping would silently DROP unmodeled-but-real
    visual knobs (``compare_prior_period``, ``top_n``, ``smooth``,
    ``stacked``, and any forward-compat field), persisting a stripped
    layout. Returning the input preserves strict validation of what we
    model while keeping everything else intact.
    """
    if not isinstance(value, dict):
        raise ValueError("layout_json must be a JSON object")
    if value == {}:
        return value
    LayoutJson.model_validate(value)
    return value


def validate_canvas_filters_json(value: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``canvas_filters_json`` payload.

    Empty dict passes through. A populated dict is validated against
    ``CanvasFilters`` and the ORIGINAL dict is returned verbatim (validate
    for side-effect only — see ``validate_layout_json`` for why we do not
    round-trip through ``model_dump``).
    """
    if not isinstance(value, dict):
        raise ValueError("canvas_filters_json must be a JSON object")
    if value == {}:
        return value
    CanvasFilters.model_validate(value)
    return value
