"""Pydantic schemas for the L4.7 audit-log read API."""
from __future__ import annotations

import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    actor_user_id: Optional[int]
    actor_email: str
    target_org_id: Optional[int]
    target_org_name: Optional[str]
    request_id: Optional[str]
    ip_address: Optional[str]
    outcome: Literal["success", "failure"]
    detail: Optional[dict[str, Any]]
    created_at: datetime.datetime


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    total: int
