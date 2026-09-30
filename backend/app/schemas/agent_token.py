"""Schemas for the agent access token API (TBD-578), ``/api/v1/agent/tokens``.

The plaintext token appears only in ``AgentTokenMintResponse.token``, once.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentScope = Literal["agent:read", "agent:write", "agent:auto"]

AUTO_MAX_EXPIRY_DAYS = 30


class AgentTokenMintRequest(BaseModel):
    """Mint body. Step-up fields mirror ``MintTokenRequest``: which one is
    required depends on the live user row (password, SSO proof, TOTP)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    scope: AgentScope
    expires_in_days: int = Field(default=30, ge=1, le=90)
    # ``agent:auto`` executes reversible writes without a confirm call, so it
    # takes a deliberate acknowledgment and a shorter life.
    acknowledge_auto: bool = False
    current_password: Optional[str] = None
    stepup_token: Optional[str] = Field(default=None, max_length=128)
    mfa_code: Optional[str] = None

    @model_validator(mode="after")
    def _auto_rules(self) -> "AgentTokenMintRequest":
        if self.scope == "agent:auto":
            if self.acknowledge_auto is not True:
                raise ValueError("agent:auto requires acknowledge_auto: true")
            if self.expires_in_days > AUTO_MAX_EXPIRY_DAYS:
                raise ValueError(
                    f"agent:auto tokens expire within {AUTO_MAX_EXPIRY_DAYS} days"
                )
        return self


class AgentTokenScopeUpdate(BaseModel):
    """PATCH body: a strictly lower scope (checked in the router)."""

    model_config = ConfigDict(extra="forbid")

    scope: AgentScope


class AgentTokenOut(BaseModel):
    """Metadata only; never the secret or the hash."""

    id: int
    name: str
    prefix: str
    scope: str
    created_at: datetime
    expires_at: datetime
    last_used_at: Optional[datetime] = None
    last_used_ip: Optional[str] = None
    status: Literal["active", "expired", "revoked", "invalidated"]


class OrgAgentTokenOut(AgentTokenOut):
    owner_user_id: int
    owner_email: str


class AgentTokenMintResponse(BaseModel):
    token: str
    id: int
    name: str
    prefix: str
    scope: str
    created_at: datetime
    expires_at: datetime
