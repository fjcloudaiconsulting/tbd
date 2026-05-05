"""Schemas for /api/v1/orgs/data endpoints (L3.1)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class OrgDataResetRequest(BaseModel):
    confirm_phrase: str = Field(
        ...,
        description='Must equal "RESET <org name>" exactly (compared after .strip(), case-sensitive).',
    )


class OrgDataResetResponse(BaseModel):
    deleted_rows_by_table: dict[str, int]
