"""Pydantic schemas for the ``/api/v1/dashboard`` endpoints.

Models the per-org saved dashboard layout (layout JSON, canvas filter JSON,
schema version). Reuses the same ``validate_layout_json`` /
``validate_canvas_filters_json`` validators that Reports uses, ensuring the
same wire-contract on both surfaces.

Architecture notes:

- ``layout_json`` and ``canvas_filters_json`` validators are imported from
  ``app.schemas.report_layout`` (the shared validator module) — not
  copy-pasted. Any fix or extension to the layout validation shape applies
  to both Reports and Dashboard automatically.
- Validators use the validate-and-return-verbatim pattern (side-effect only,
  no ``model_dump`` round-trip). This prevents the #424 regression where
  ``extra="ignore"`` widget configs silently strip unmodelled visual knobs
  (``compare_prior_period``, ``top_n``, ``smooth``, ``stacked``, etc.).
- ``DashboardUpdate`` uses ``extra="forbid"`` so unknown keys are rejected
  (matches ``ReportUpdate`` behaviour).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.report_layout import (
    validate_canvas_filters_json,
    validate_layout_json,
)


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
        return validate_layout_json(v)

    @field_validator("canvas_filters_json")
    @classmethod
    def _check_canvas_filters(
        cls, v: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if v is None:
            return v
        return validate_canvas_filters_json(v)
