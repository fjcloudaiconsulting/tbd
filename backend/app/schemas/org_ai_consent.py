"""Pydantic schemas for AI consents (PR1 follow-up)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


CONSENT_VERSION_MAX_LENGTH = 40


class ConsentCreate(BaseModel):
    """Append-only — no UPDATE. Creating a new row is how revocation
    works too (pass ``revoked=True`` to record a withdrawal row)."""

    model_config = ConfigDict(extra="forbid")

    consent_version: str = Field(
        min_length=1, max_length=CONSENT_VERSION_MAX_LENGTH
    )
    allow_training: bool = False
    allow_rag: bool = False
    allow_telemetry: bool = False
    revoked: bool = False


class ConsentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    allow_training: bool
    allow_rag: bool
    allow_telemetry: bool
    consent_version: str
    consented_by_user_id: Optional[int]
    consented_at: datetime
    revoked_at: Optional[datetime]


class ConsentSnapshotResponse(BaseModel):
    """Effective consent state derived from the latest non-revoked row."""

    allow_training: bool
    allow_rag: bool
    allow_telemetry: bool
    consent_version: Optional[str] = None
    consented_by_user_id: Optional[int] = None
    consented_at: Optional[datetime] = None
    has_consent: bool
