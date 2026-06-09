"""Pydantic schemas for the ``/api/v1/reports`` CRUD endpoints.

The schema models the saved-report shape (layout JSON, canvas filter
JSON, name, visibility). The shape of the AST executed against
``/api/v1/reports/query`` is in ``backend/app/schemas/reports_query.py``.

Architect-locked decisions:

- ``visibility`` is a closed enum (``private`` / ``org``).
- ``name`` required, length bounded.
- ``layout_json`` + ``canvas_filters_json`` are strictly validated on
  write against ``app.schemas.report_layout`` (the populated shape mirrors
  ``frontend/lib/reports/types.ts``). An empty dict (a blank / new report)
  is allowed; any populated-but-malformed layout is rejected 422.
- Create / Update reject unknown keys via ``extra="forbid"``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.report import ReportVisibility
from app.schemas.report_layout import (
    validate_canvas_filters_json,
    validate_layout_json,
)


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 500


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH)
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    visibility: ReportVisibility = ReportVisibility.PRIVATE
    layout_json: dict[str, Any] = Field(default_factory=dict)
    canvas_filters_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("layout_json")
    @classmethod
    def _check_layout(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_layout_json(v)

    @field_validator("canvas_filters_json")
    @classmethod
    def _check_canvas_filters(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_canvas_filters_json(v)


class ReportUpdate(BaseModel):
    """Partial update. Every field optional; absent keys leave the DB
    column alone. ``visibility`` change is allowed when the caller has
    edit rights (enforced at the router).
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(
        default=None, min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH
    )
    description: Optional[str] = Field(
        default=None, max_length=DESCRIPTION_MAX_LENGTH
    )
    visibility: Optional[ReportVisibility] = None
    layout_json: Optional[dict[str, Any]] = None
    canvas_filters_json: Optional[dict[str, Any]] = None

    @field_validator("layout_json")
    @classmethod
    def _check_layout(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        # ``None`` (absent key) is left for the router's explicit-null
        # guard; only a present dict is validated here.
        if v is None:
            return v
        return validate_layout_json(v)

    @field_validator("canvas_filters_json")
    @classmethod
    def _check_canvas_filters(
        cls, v: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if v is None:
            return v
        return validate_canvas_filters_json(v)


class ReportTemplate(BaseModel):
    """A starter report template returned by
    ``GET /api/v1/reports/templates``. The frontend "Use template" action
    POSTs ``layout_json`` / ``canvas_filters_json`` to the create endpoint.
    """

    key: str
    name: str
    description: str
    layout_json: dict[str, Any]
    canvas_filters_json: dict[str, Any]


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_user_id: int
    org_id: int
    visibility: ReportVisibility
    name: str
    description: Optional[str] = None
    layout_json: dict[str, Any]
    canvas_filters_json: dict[str, Any]
    schema_version: int
    created_at: datetime
    updated_at: datetime


class ReportVersionSummary(BaseModel):
    """Lightweight version-history row for the version list endpoint.

    Intentionally omits the full ``layout_json`` / ``canvas_filters_json``
    payload; the list only needs to render selectable history entries.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    is_original: bool
    created_at: datetime
