"""Pydantic schemas for L3.8 — org invitations and members."""
from __future__ import annotations

import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

from app.schemas.auth import USERNAME_MAX_LENGTH


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"]


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=1)
    # Lenient at the request layer so reactivation doesn't reject a
    # legacy username that pre-dates the strict regex (introduced in
    # PR #70). Strict validation is applied in `invitation_service`
    # only when creating a NEW user.
    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH)
    password: str = Field(min_length=8, max_length=128)


class InvitationResponse(BaseModel):
    id: int
    email: str
    role: Literal["owner", "admin", "member"]
    created_at: datetime.datetime
    expires_at: datetime.datetime
    inviter_username: Optional[str] = None
    status: Literal["pending"] = "pending"


class InvitationPreviewResponse(BaseModel):
    org_name: str
    email: str
    role: Literal["owner", "admin", "member"]
    is_reactivation: bool
    existing_username: Optional[str] = None


class MemberResponse(BaseModel):
    id: int
    username: str
    email: str
    role: Literal["owner", "admin", "member"]
    is_active: bool
