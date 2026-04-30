"""Pydantic schemas for L3.8 — org invitations and members."""
from __future__ import annotations

import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

from app.schemas.auth import (
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
)


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"]


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=1)
    username: str = Field(
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        pattern=USERNAME_PATTERN,
    )
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
