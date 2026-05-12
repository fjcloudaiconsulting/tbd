"""Schemas for the admin user-management endpoints."""
from __future__ import annotations

from pydantic import BaseModel, Field


class UserMergeRequest(BaseModel):
    """Body for ``POST /api/v1/admin/users/merge``."""

    source_user_id: int = Field(gt=0, description="row to delete after merge")
    target_user_id: int = Field(gt=0, description="row that survives")


class UserMergeResponse(BaseModel):
    """Per-table count of rows reassigned during the merge."""

    source_user_id: int
    target_user_id: int
    counts: dict[str, int]
