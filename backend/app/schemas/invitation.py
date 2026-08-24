"""Pydantic schemas for L3.8 — org invitations and members."""
from __future__ import annotations

import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

from app.schemas.auth import USERNAME_MAX_LENGTH


class InvitationCreateRequest(BaseModel):
    # ⚠ max_length MUST match the column this reaches (``String(120)``).
    # ``EmailStr`` alone accepts 254 characters, so a syntactically VALID
    # longer address reached the INSERT and raised an unhandled 500
    # (MySQL DataError 1406 under STRICT_TRANS_TABLES). ⚠⚠ SQLite does not
    # enforce ``VARCHAR(n)``, so the shards cannot see this class at all.
    email: EmailStr = Field(max_length=120)
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
