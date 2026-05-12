"""Pydantic schemas for L4.3 admin org management."""
from __future__ import annotations

import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SubscriptionUpdateRequest(BaseModel):
    plan_id: Optional[int] = None
    status: Optional[Literal["trialing", "active", "past_due", "canceled"]] = None
    trial_end: Optional[datetime.date] = None
    current_period_end: Optional[datetime.date] = None


class OrgDeleteRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=200)


# ── L4.4 member management ────────────────────────────────────────────────


class AdminMemberResponse(BaseModel):
    """Member row returned by /admin/orgs/{id}/members. Superset of the
    org-side `MemberResponse`: includes `email_verified` (operator
    diagnostics) and `is_superadmin` (so the UI can disable mutation
    affordances)."""

    id: int
    username: str
    email: str
    role: Literal["owner", "admin", "member"]
    is_active: bool
    email_verified: bool
    is_superadmin: bool
    created_at: Optional[str] = None


class AdminMemberUpdateRequest(BaseModel):
    """Partial update for /admin/orgs/{id}/members/{user_id}.

    Both fields optional; the service rejects an all-None body with
    400. The role enum here intentionally omits platform-level roles
    (e.g. "superadmin") — they're not assignable through this
    surface."""

    role: Optional[Literal["owner", "admin", "member"]] = None
    is_active: Optional[bool] = None
