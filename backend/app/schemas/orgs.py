"""Schemas for tenant-org self-management endpoints (Track D).

Distinct from ``schemas/admin_orgs.py`` (the platform-superadmin
surface). The ``rename`` endpoint here is owner-scoped to the
current tenant; the admin surface gets a parallel rename later.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


class OrgRenameRequest(BaseModel):
    """Body for ``PATCH /api/v1/orgs/{org_id}/rename``.

    Whitespace policy:
        - Trim leading/trailing whitespace.
        - Collapse runs of internal whitespace to a single space.
        - Reject control characters (\\x00-\\x1F + \\x7F).

    The DB column is ``VARCHAR(200)`` but we cap user input at 80 to
    leave room for cosmetic suffixes if needed and to keep the rename
    UI tight. The 200-char column stays in place (no migration).
    """

    name: str = Field(..., min_length=1, max_length=80)

    @field_validator("name", mode="after")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = v.strip()
        v = re.sub(r"\s+", " ", v)
        if not v:
            raise ValueError("Name cannot be empty after trimming whitespace")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("Name cannot contain control characters")
        return v


class OrgResponse(BaseModel):
    """Read shape returned by the rename endpoint. Mirrors the
    ``Organization`` ORM row's identity-shaping fields without
    leaking the timestamps or other internal state.
    """

    id: int
    name: str
    billing_cycle_day: int

    model_config = {"from_attributes": True}
