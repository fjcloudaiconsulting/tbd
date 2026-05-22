"""Pydantic schemas for the ``/api/v1/reports`` CRUD endpoints.

The schema models the saved-report shape (layout JSON, canvas filter
JSON, name, visibility). The shape of the AST executed against
``/api/v1/reports/query`` is in ``backend/app/schemas/reports_query.py``.

Architect-locked decisions:

- ``visibility`` is a closed enum (``private`` / ``org``).
- ``name`` required, length bounded.
- ``layout_json`` + ``canvas_filters_json`` are passthrough dicts in
  PR1. PR2 lands the strict layout-schema validator; for now we only
  require well-formed JSON objects.
- Create / Update reject unknown keys via ``extra="forbid"``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.report import ReportVisibility


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
