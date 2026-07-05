from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Organization

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_NOOP = "noop"


@dataclass
class JobResult:
    outcome: str
    counts: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def noop(cls) -> "JobResult":
        return cls(outcome=OUTCOME_NOOP)

    @classmethod
    def ok(cls, counts: dict[str, Any] | None = None) -> "JobResult":
        return cls(outcome=OUTCOME_SUCCESS, counts=counts or {})

    @classmethod
    def failed(cls, error: str) -> "JobResult":
        return cls(outcome=OUTCOME_FAILURE, error=error)


@runtime_checkable
class ScheduledJob(Protocol):
    job_type: str
    setting_key: str

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool: ...
    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult: ...
