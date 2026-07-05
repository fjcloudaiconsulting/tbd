"""Pydantic schemas for the per-org scheduler settings endpoint."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SchedulerSettingsResponse(BaseModel):
    automate_recurring_generation: bool
    automate_billing_close: bool
    billing_close_reminder_lead_days: int


class SchedulerSettingsUpdate(BaseModel):
    automate_recurring_generation: bool | None = None
    automate_billing_close: bool | None = None
    billing_close_reminder_lead_days: int | None = Field(default=None, ge=0, le=31)
