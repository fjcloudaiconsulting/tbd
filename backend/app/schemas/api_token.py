"""Request/response schemas for the superadmin PAT management API (spec §8).

The plaintext token appears in exactly one place across the whole system:
``MintTokenResponse.token``, returned once by ``POST /``. It is never
persisted, never logged, and never written to an audit ``detail`` (SEC-R5).
Every other schema here is metadata-only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class MintTokenRequest(BaseModel):
    """Body for ``POST /api/v1/system/api-tokens`` — mint.

    Step-up proofs (``current_password`` / ``stepup_token`` / ``mfa_code``)
    are all optional at the schema layer because *which* proof is required
    depends on the acting operator's auth shape (password-set vs SSO,
    MFA-enabled or not). The router resolves the exact requirement against
    the live user row (spec §8) and returns 401 when the needed proof is
    missing or wrong — the schema never leaks which factor an operator has.
    """

    name: str = Field(min_length=1, max_length=100)
    scope: Literal["read", "write"]
    # Cap is ALSO enforced server-side against the live setting; the schema
    # bound is a fast-fail so an obviously-bad value 422s before any step-up
    # work. ``le`` is read from settings at class-eval time; the router
    # re-checks defensively so a runtime setting change can't be bypassed.
    expires_in_days: int = Field(
        default=settings.api_token_default_expiry_days,
        ge=1,
    )
    current_password: Optional[str] = None
    stepup_token: Optional[str] = None
    mfa_code: Optional[str] = None

    @field_validator("expires_in_days")
    @classmethod
    def _cap_expiry(cls, v: int) -> int:
        # Server-side hard cap (SEC-R7): never trust the client. Reading the
        # live setting here (rather than a static ``le=``) keeps the bound
        # authoritative even if the setting is monkeypatched in a test or
        # changed at deploy time.
        if v < 1 or v > settings.api_token_max_expiry_days:
            raise ValueError(
                f"expires_in_days must be between 1 and "
                f"{settings.api_token_max_expiry_days}"
            )
        return v


class MintTokenResponse(BaseModel):
    """Reveal-once mint response. ``token`` is the ONLY place the plaintext
    ever appears (SEC-R5); the handler also sets ``Cache-Control: no-store``.
    """

    token: str
    id: int
    name: str
    prefix: str
    scope: str
    created_at: datetime
    expires_at: datetime


class ApiTokenOut(BaseModel):
    """Metadata-only list/detail row. NEVER carries the secret or the hash."""

    id: int
    name: str
    prefix: str
    scope: str
    created_at: datetime
    expires_at: datetime
    last_used_at: Optional[datetime] = None
    status: Literal["active", "expired", "revoked"]
