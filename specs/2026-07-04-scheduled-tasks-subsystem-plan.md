# Scheduled-tasks Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add time-driven automatic execution of two org-scoped jobs (recurring-due generation and billing-period close), each per-org switchable off, observable in `/admin/audit`, with dual-channel member notifications.

**Architecture:** A single background asyncio ticker in the FastAPI lifespan wakes every N minutes, takes a Redis `SET NX EX` lock (single-runner), and runs every enabled+due job for every org. "Due" is derived from durable domain state so downtime is caught up on the next boot and re-runs are no-ops. Each job owns its own work + notifications; a thin runner isolates per-org/per-job failures. Observability reuses `audit_events`; notifications reuse the existing dual-channel stack.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Redis (via `app/redis_client.py`), Next.js 16 / React 19 / SWR frontend, pytest + vitest.

## Global Constraints

- All API routes prefixed `/api/v1/`. New router uses `APIRouter(prefix="/api/v1/scheduler")`.
- Auth on every endpoint via `get_current_user`; the settings write path is admin-gated.
- Org-scoped: every query filters by `org_id`.
- OrgSetting keys use a new `scheduler.` namespace, distinct from the RESERVED `feature.` namespace. Do NOT route these through the generic user-facing settings writer.
- Enum values stored lowercase (existing convention); we store booleans as the strings `"true"`/`"false"` in `org_settings.value` (Text).
- System audit rows: `actor_user_id=None`, `actor_email="system"`.
- No new DB tables, no migration. `OrgSetting`, `audit_events`, notification tables reused as-is.
- No em-dashes in user-facing copy (notification titles/bodies, UI strings).
- Tests run against an isolated compose project: every command carries `-p team-sched`. Never `./pfv migrate` from this session.
- CC-bill close is OUT of scope (deferred to v2 per the design spec).

**Spec:** `specs/2026-07-04-scheduled-tasks-subsystem-design.md`

**Test command prefix (backend):**
`docker compose -p team-sched up -d backend mysql redis` (once), then
`docker compose -p team-sched exec backend pytest <path> -v`
Service-level tests use a self-contained in-memory SQLite `session_factory` fixture (matches `tests/services/test_billing_service*.py`), so most run without the stack too.

---

### Task 1: Scheduler config flags

**Files:**
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_scheduler_config.py`

**Interfaces:**
- Produces: `Settings.scheduler_enabled: bool`, `Settings.scheduler_tick_seconds: int`, `Settings.scheduler_lock_ttl_seconds: int`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_scheduler_config.py
from app.config import Settings


def test_scheduler_defaults():
    s = Settings(_env_file=None)
    assert s.scheduler_enabled is True
    assert s.scheduler_tick_seconds == 900
    assert s.scheduler_lock_ttl_seconds == 600


def test_scheduler_overrides_from_env(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "60")
    s = Settings(_env_file=None)
    assert s.scheduler_enabled is False
    assert s.scheduler_tick_seconds == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/test_scheduler_config.py -v`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'scheduler_enabled'`).

- [ ] **Step 3: Add the fields**

Add to the `Settings` class body in `backend/app/config.py` (near other feature flags), keeping the file's existing field style:

```python
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 900
    scheduler_lock_ttl_seconds: int = 600
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/test_scheduler_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_scheduler_config.py
git commit -m "feat(scheduler): config flags for the tick loop"
```

---

### Task 2: Scheduler org-settings accessors

**Files:**
- Create: `backend/app/services/scheduler/__init__.py` (empty)
- Create: `backend/app/services/scheduler/org_settings.py`
- Test: `backend/tests/services/test_scheduler_org_settings.py`

**Interfaces:**
- Consumes: `settings_service.get_org_setting(db, org_id, key, default)`, `OrgSetting` model.
- Produces:
  - Constants `AUTOMATE_RECURRING_KEY = "scheduler.automate_recurring_generation"`, `AUTOMATE_BILLING_KEY = "scheduler.automate_billing_close"`, `REMINDER_LEAD_DAYS_KEY = "scheduler.billing_close_reminder_lead_days"`.
  - `async def get_bool(db, org_id: int, key: str) -> bool`
  - `async def get_reminder_lead_days(db, org_id: int) -> int`
  - `async def set_value(db, org_id: int, key: str, value: str) -> None` (upsert; caller commits)
  - `async def get_all(db, org_id: int) -> dict` returning `{"automate_recurring_generation": bool, "automate_billing_close": bool, "billing_close_reminder_lead_days": int}`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_org_settings.py
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler import org_settings as so


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_factory() as db:
        o = Organization(name="Acme", billing_cycle_day=1)
        db.add(o)
        await db.commit()
        await db.refresh(o)
        return o


async def test_defaults_when_unset(session_factory, org):
    async with session_factory() as db:
        assert await so.get_bool(db, org.id, so.AUTOMATE_RECURRING_KEY) is True
        assert await so.get_bool(db, org.id, so.AUTOMATE_BILLING_KEY) is True
        assert await so.get_reminder_lead_days(db, org.id) == 3


async def test_set_and_read_back(session_factory, org):
    async with session_factory() as db:
        await so.set_value(db, org.id, so.AUTOMATE_BILLING_KEY, "false")
        await so.set_value(db, org.id, so.REMINDER_LEAD_DAYS_KEY, "7")
        await db.commit()
    async with session_factory() as db:
        assert await so.get_bool(db, org.id, so.AUTOMATE_BILLING_KEY) is False
        assert await so.get_reminder_lead_days(db, org.id) == 7
        allv = await so.get_all(db, org.id)
        assert allv == {
            "automate_recurring_generation": True,
            "automate_billing_close": False,
            "billing_close_reminder_lead_days": 7,
        }


async def test_reminder_lead_days_clamped_on_garbage(session_factory, org):
    async with session_factory() as db:
        await so.set_value(db, org.id, so.REMINDER_LEAD_DAYS_KEY, "not-a-number")
        await db.commit()
    async with session_factory() as db:
        assert await so.get_reminder_lead_days(db, org.id) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_org_settings.py -v`
Expected: FAIL (`ModuleNotFoundError: app.services.scheduler`).

- [ ] **Step 3: Implement the accessors**

```python
# backend/app/services/scheduler/__init__.py
```

```python
# backend/app/services/scheduler/org_settings.py
"""Typed per-org scheduler settings, stored under the ``scheduler.`` OrgSetting
namespace. Kept out of the generic user-facing settings writer so the RESERVED
``feature.`` namespace guard is untouched.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import OrgSetting
from app.services.settings_service import get_org_setting

AUTOMATE_RECURRING_KEY = "scheduler.automate_recurring_generation"
AUTOMATE_BILLING_KEY = "scheduler.automate_billing_close"
REMINDER_LEAD_DAYS_KEY = "scheduler.billing_close_reminder_lead_days"

_BOOL_DEFAULTS = {AUTOMATE_RECURRING_KEY: "true", AUTOMATE_BILLING_KEY: "true"}
_REMINDER_DEFAULT = 3
_REMINDER_MIN, _REMINDER_MAX = 0, 31


async def get_bool(db: AsyncSession, org_id: int, key: str) -> bool:
    raw = await get_org_setting(db, org_id, key, _BOOL_DEFAULTS.get(key, "false"))
    return str(raw).strip().lower() == "true"


async def get_reminder_lead_days(db: AsyncSession, org_id: int) -> int:
    raw = await get_org_setting(db, org_id, REMINDER_LEAD_DAYS_KEY, str(_REMINDER_DEFAULT))
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return _REMINDER_DEFAULT
    return max(_REMINDER_MIN, min(_REMINDER_MAX, val))


async def set_value(db: AsyncSession, org_id: int, key: str, value: str) -> None:
    """Upsert a single scheduler setting. Caller is responsible for commit."""
    row = (
        await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == org_id, OrgSetting.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))
    else:
        row.value = value


async def get_all(db: AsyncSession, org_id: int) -> dict:
    return {
        "automate_recurring_generation": await get_bool(db, org_id, AUTOMATE_RECURRING_KEY),
        "automate_billing_close": await get_bool(db, org_id, AUTOMATE_BILLING_KEY),
        "billing_close_reminder_lead_days": await get_reminder_lead_days(db, org_id),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_org_settings.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/ backend/tests/services/test_scheduler_org_settings.py
git commit -m "feat(scheduler): typed per-org settings accessors"
```

---

### Task 3: Scheduler settings API (GET/PUT)

**Files:**
- Create: `backend/app/schemas/scheduler.py`
- Create: `backend/app/routers/scheduler.py`
- Modify: `backend/app/main.py` (register router)
- Test: `backend/tests/routers/test_scheduler_settings_api.py`

**Interfaces:**
- Consumes: `org_settings` accessors from Task 2; `get_current_user`; the existing admin dependency (see below).
- Produces: `GET /api/v1/scheduler/settings` → `SchedulerSettingsResponse`; `PUT /api/v1/scheduler/settings` (admin) → `SchedulerSettingsResponse`.

**Note on the admin dependency:** find the existing admin guard used by sibling org-scoped admin writes (grep `require_admin` / `require_org_admin` in `app/routers/settings.py` and `app/auth/org_permissions.py`) and reuse the SAME dependency the manual `close_period` route in `settings.py:306` uses. Do not invent a new one.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/routers/test_scheduler_settings_api.py
# Uses the project's existing httpx client + auth fixtures (mirror the
# nearest tests/routers/test_settings*.py: same client fixture, same
# helper to register the first user = admin, same Bearer header helper).
import pytest


async def test_get_returns_defaults(client, admin_auth_headers):
    r = await client.get("/api/v1/scheduler/settings", headers=admin_auth_headers)
    assert r.status_code == 200
    assert r.json() == {
        "automate_recurring_generation": True,
        "automate_billing_close": True,
        "billing_close_reminder_lead_days": 3,
    }


async def test_put_updates_subset(client, admin_auth_headers):
    r = await client.put(
        "/api/v1/scheduler/settings",
        headers=admin_auth_headers,
        json={"automate_billing_close": False, "billing_close_reminder_lead_days": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["automate_billing_close"] is False
    assert body["billing_close_reminder_lead_days"] == 5
    assert body["automate_recurring_generation"] is True  # untouched


async def test_put_rejects_out_of_range_lead_days(client, admin_auth_headers):
    r = await client.put(
        "/api/v1/scheduler/settings",
        headers=admin_auth_headers,
        json={"billing_close_reminder_lead_days": 99},
    )
    assert r.status_code == 422


async def test_put_forbidden_for_non_admin(client, member_auth_headers):
    r = await client.put(
        "/api/v1/scheduler/settings",
        headers=member_auth_headers,
        json={"automate_billing_close": False},
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/routers/test_scheduler_settings_api.py -v`
Expected: FAIL (404 / router not registered).

- [ ] **Step 3: Implement schema + router + registration**

```python
# backend/app/schemas/scheduler.py
from pydantic import BaseModel, Field


class SchedulerSettingsResponse(BaseModel):
    automate_recurring_generation: bool
    automate_billing_close: bool
    billing_close_reminder_lead_days: int


class SchedulerSettingsUpdate(BaseModel):
    automate_recurring_generation: bool | None = None
    automate_billing_close: bool | None = None
    billing_close_reminder_lead_days: int | None = Field(default=None, ge=0, le=31)
```

```python
# backend/app/routers/scheduler.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_current_user
# Reuse the SAME admin guard the manual close_period route uses (Task 3 note).
from app.auth.org_permissions import require_admin  # adjust import to the real symbol
from app.models.user import User
from app.schemas.scheduler import SchedulerSettingsResponse, SchedulerSettingsUpdate
from app.services.scheduler import org_settings as so

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])


@router.get("/settings", response_model=SchedulerSettingsResponse)
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await so.get_all(db, current_user.org_id)


@router.put("/settings", response_model=SchedulerSettingsResponse)
async def put_settings(
    body: SchedulerSettingsUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    org_id = current_user.org_id
    if body.automate_recurring_generation is not None:
        await so.set_value(db, org_id, so.AUTOMATE_RECURRING_KEY,
                           "true" if body.automate_recurring_generation else "false")
    if body.automate_billing_close is not None:
        await so.set_value(db, org_id, so.AUTOMATE_BILLING_KEY,
                           "true" if body.automate_billing_close else "false")
    if body.billing_close_reminder_lead_days is not None:
        await so.set_value(db, org_id, so.REMINDER_LEAD_DAYS_KEY,
                           str(body.billing_close_reminder_lead_days))
    await db.commit()
    return await so.get_all(db, org_id)
```

Register in `backend/app/main.py` alongside the other `app.include_router(...)` calls:

```python
from app.routers import scheduler as scheduler_router
app.include_router(scheduler_router.router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/routers/test_scheduler_settings_api.py -v`
Expected: PASS (4 tests). If the admin-guard import symbol differs, fix the import to the real one and re-run.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/scheduler.py backend/app/routers/scheduler.py backend/app/main.py backend/tests/routers/test_scheduler_settings_api.py
git commit -m "feat(scheduler): org settings GET/PUT endpoint"
```

---

### Task 4: JobResult + ScheduledJob protocol

**Files:**
- Create: `backend/app/services/scheduler/base.py`
- Test: `backend/tests/services/test_scheduler_base.py`

**Interfaces:**
- Produces:
  - `@dataclass class JobResult: outcome: str; counts: dict[str, Any] = {}; error: str | None = None` with class-level helpers `JobResult.noop()`, `JobResult.ok(counts)`, `JobResult.failed(error)`.
  - `class ScheduledJob(Protocol): job_type: str; setting_key: str; async def is_due(self, db, org, today) -> bool; async def run(self, db, org, today) -> JobResult`.
  - `OUTCOME_SUCCESS = "success"`, `OUTCOME_FAILURE = "failure"`, `OUTCOME_NOOP = "noop"`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_base.py
from app.services.scheduler.base import JobResult, OUTCOME_NOOP, OUTCOME_SUCCESS


def test_jobresult_helpers():
    assert JobResult.noop().outcome == OUTCOME_NOOP
    ok = JobResult.ok({"generated": 2})
    assert ok.outcome == OUTCOME_SUCCESS
    assert ok.counts == {"generated": 2}
    assert ok.error is None
    bad = JobResult.failed("boom")
    assert bad.outcome == "failure"
    assert bad.error == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_base.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement base**

```python
# backend/app/services/scheduler/base.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_base.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/base.py backend/tests/services/test_scheduler_base.py
git commit -m "feat(scheduler): JobResult + ScheduledJob protocol"
```

---

### Task 5: Scheduler audit helpers

**Files:**
- Create: `backend/app/services/scheduler/audit.py`
- Test: `backend/tests/services/test_scheduler_audit.py`

**Interfaces:**
- Consumes: `audit_service.record_audit_event(session_factory, *, event_type, actor_user_id, actor_email, target_org_id, target_org_name, request_id, ip_address, outcome, detail)`; the engine-wide `async_session` factory from `app/database.py`; `list_audit_events` or a direct query for dedup.
- Produces:
  - `async def record_run(*, job_type: str, outcome: str, org: Organization, detail: dict) -> int | None` (writes `event_type=f"scheduler.{job_type}.{outcome}"`, outcome ∈ {success, failure}).
  - `async def record_reminder(*, org: Organization, period_start: datetime.date, detail: dict) -> int | None` (writes `event_type="scheduler.billing_close.reminder"`, outcome=success, `detail["period_start"]=period_start.isoformat()`).
  - `async def reminder_already_sent(db, org_id: int, period_start: datetime.date) -> bool`.

The audit helpers use `record_audit_event`'s OWN session factory (independent txn), so they must be passed the factory; import it as `from app.database import async_session`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_audit.py
from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization
from app.services.scheduler import audit as sched_audit


@pytest_asyncio.fixture
async def session_factory(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # record_run/record_reminder open their own session via app.database.async_session
    monkeypatch.setattr(sched_audit, "async_session", factory)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_factory() as db:
        o = Organization(name="Acme", billing_cycle_day=1)
        db.add(o)
        await db.commit()
        await db.refresh(o)
        return o


async def test_record_run_writes_scheduler_event(session_factory, org):
    await sched_audit.record_run(
        job_type="recurring_generation", outcome="success", org=org,
        detail={"generated": 3},
    )
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "scheduler.recurring_generation.success"
    assert rows[0].actor_email == "system"
    assert rows[0].actor_user_id is None
    assert rows[0].target_org_id == org.id
    assert rows[0].detail == {"generated": 3}


async def test_reminder_dedup(session_factory, org):
    period = datetime.date(2026, 8, 1)
    assert await _sent(session_factory, org, period) is False
    await sched_audit.record_reminder(org=org, period_start=period, detail={})
    assert await _sent(session_factory, org, period) is True
    # a different period is independent
    assert await _sent(session_factory, org, datetime.date(2026, 9, 1)) is False


async def _sent(session_factory, org, period):
    async with session_factory() as db:
        return await sched_audit.reminder_already_sent(db, org.id, period)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_audit.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement audit helpers**

```python
# backend/app/services/scheduler/audit.py
from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit_event import AuditEvent
from app.models.user import Organization
from app.services.audit_service import record_audit_event

REMINDER_EVENT_TYPE = "scheduler.billing_close.reminder"


async def record_run(*, job_type: str, outcome: str, org: Organization, detail: dict[str, Any]) -> int | None:
    return await record_audit_event(
        async_session,
        event_type=f"scheduler.{job_type}.{outcome}",
        actor_user_id=None,
        actor_email="system",
        target_org_id=org.id,
        target_org_name=org.name,
        request_id=None,
        ip_address=None,
        outcome=outcome,  # "success" | "failure"
        detail=detail,
    )


async def record_reminder(*, org: Organization, period_start: datetime.date, detail: dict[str, Any]) -> int | None:
    payload = dict(detail)
    payload["period_start"] = period_start.isoformat()
    return await record_audit_event(
        async_session,
        event_type=REMINDER_EVENT_TYPE,
        actor_user_id=None,
        actor_email="system",
        target_org_id=org.id,
        target_org_name=org.name,
        request_id=None,
        ip_address=None,
        outcome="success",
        detail=payload,
    )


async def reminder_already_sent(db: AsyncSession, org_id: int, period_start: datetime.date) -> bool:
    iso = period_start.isoformat()
    rows = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == REMINDER_EVENT_TYPE,
                AuditEvent.target_org_id == org_id,
            )
        )
    ).scalars().all()
    return any((r.detail or {}).get("period_start") == iso for r in rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_audit.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/audit.py backend/tests/services/test_scheduler_audit.py
git commit -m "feat(scheduler): audit-run + reminder-dedup helpers"
```

---

### Task 6: All-members notification fan-out + templates

**Files:**
- Modify: `backend/app/services/notification_service.py` (add members fan-out)
- Modify: `backend/app/services/notification_templates.py` (add 3 templates)
- Test: `backend/tests/services/test_scheduler_notifications.py`

**Interfaces:**
- Consumes: existing `dispatch_notification`, `NotificationCategory.org_activity`, the members-selection SQL pattern from `dispatch_notification_to_org_admins` (mirror it but select ALL active users of the org regardless of role).
- Produces:
  - `async def dispatch_notification_to_org_members(db, *, org_id, category, event_type, title, body, link_url=None, audit_event_id=None) -> int` (count dispatched).
  - `def scheduler_recurring_generated(*, generated: int, settled: int) -> tuple[str, str, str | None]`
  - `def scheduler_billing_close_reminder(*, close_date, days_until) -> tuple[str, str, str | None]`
  - `def scheduler_billing_closed(*, new_period_start) -> tuple[str, str, str | None]`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_notifications.py
# Follow the existing tests/services/test_notification_service*.py setup:
# same in-memory session_factory, same helpers to create an org with N
# active users and to set a user's org_activity preference off.
import pytest
from app.models.notification import NotificationCategory
from app.services import notification_service as ns
from app.services import notification_templates as nt


def test_templates_have_no_em_dashes():
    for title, body, _ in (
        nt.scheduler_recurring_generated(generated=2, settled=1),
        nt.scheduler_billing_close_reminder(close_date=__import__("datetime").date(2026, 8, 1), days_until=3),
        nt.scheduler_billing_closed(new_period_start=__import__("datetime").date(2026, 8, 1)),
    ):
        assert "—" not in title and "—" not in body
        assert title and body


async def test_fanout_reaches_all_members_and_respects_optout(session_factory, org_with_members):
    org, users, opted_out_user = org_with_members  # 3 users, 1 opted out of org_activity
    async with session_factory() as db:
        n = await ns.dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.org_activity,
            event_type="scheduler.recurring_generation.success",
            title="t", body="b",
        )
        await db.commit()
    assert n == 2  # the opted-out user is skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_notifications.py -v`
Expected: FAIL (missing functions).

- [ ] **Step 3: Implement fan-out + templates**

In `notification_service.py`, add below `dispatch_notification_to_org_admins` (mirror its savepoint-per-recipient structure, but the SELECT drops the role filter):

```python
async def dispatch_notification_to_org_members(
    db: AsyncSession,
    *,
    org_id: int,
    category: NotificationCategory,
    event_type: str,
    title: str,
    body: str,
    link_url: Optional[str] = None,
    audit_event_id: Optional[int] = None,
) -> int:
    """Fan out to every ACTIVE user of ``org_id`` (all roles). Per-user
    failures are savepoint-scoped and swallowed (best-effort), exactly like
    dispatch_notification_to_org_admins. Returns the count actually written
    (preference opt-outs are not counted)."""
    result = await db.execute(
        select(User.id).where(User.org_id == org_id, User.is_active == True)  # noqa: E712
    )
    user_ids = [row[0] for row in result.all()]
    written = 0
    for uid in user_ids:
        try:
            async with db.begin_nested():
                row = await dispatch_notification(
                    db, user_id=uid, category=category, event_type=event_type,
                    title=title, body=body, link_url=link_url, audit_event_id=audit_event_id,
                )
            if row is not None:
                written += 1
        except Exception:  # noqa: BLE001 — best-effort fanout
            await logger.awarning("notification.fanout.member_failed", user_id=uid, event_type=event_type)
    return written
```

(Confirm `User` and `select` are already imported in the module; add imports if missing.)

In `notification_templates.py`, add:

```python
import datetime  # if not already imported


def scheduler_recurring_generated(*, generated: int, settled: int) -> tuple[str, str, Optional[str]]:
    """Copy for scheduler.recurring_generation.success (org_activity)."""
    title = "Recurring transactions generated"
    body = (
        f"We added {generated} scheduled transaction(s) to your ledger "
        f"({settled} already settled). Review them on your transactions page."
    )
    return (title, body, "/transactions")


def scheduler_billing_close_reminder(
    *, close_date: datetime.date, days_until: int
) -> tuple[str, str, Optional[str]]:
    """Copy for scheduler.billing_close.reminder (org_activity)."""
    title = "Your budget period closes soon"
    body = (
        f"Your current budget period closes on {close_date.isoformat()} "
        f"(in {days_until} day(s)). A new period will open automatically."
    )
    return (title, body, "/budgets")


def scheduler_billing_closed(*, new_period_start: datetime.date) -> tuple[str, str, Optional[str]]:
    """Copy for scheduler.billing_close.success (org_activity)."""
    title = "Your budget period closed"
    body = (
        f"Your budget period was closed and a new one started on "
        f"{new_period_start.isoformat()}."
    )
    return (title, body, "/budgets")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_notifications.py -v`
Expected: PASS. (If `org_with_members` fixture does not exist, add it to this test module mirroring the org+users seed in the existing notification-service test file.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/notification_service.py backend/app/services/notification_templates.py backend/tests/services/test_scheduler_notifications.py
git commit -m "feat(scheduler): all-members notification fan-out + templates"
```

---

### Task 7: Recurring-generation job

**Files:**
- Create: `backend/app/services/scheduler/jobs/__init__.py` (empty)
- Create: `backend/app/services/scheduler/jobs/recurring_generation.py`
- Test: `backend/tests/services/test_scheduler_job_recurring.py`

**Interfaces:**
- Consumes: `recurring_service.generate_due_transactions(db, org_id) -> dict` (keys: generated, settled, pending, period_end); `billing_service.current_cycle_window`; `RecurringTransaction` model; `sched_audit.record_run`; `notification_service.dispatch_notification_to_org_members`; templates.
- Produces: `class RecurringGenerationJob` with `job_type="recurring_generation"`, `setting_key=org_settings.AUTOMATE_RECURRING_KEY`, `is_due`, `run`.

**Behavior:** `is_due` = any active template has `next_due_date <= period_end` of the current cycle. `run` = call `generate_due_transactions`, commit, then (if generated+settled > 0) record audit success + notify members; else return `JobResult.noop()` and write NO audit row.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_job_recurring.py
from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler.base import OUTCOME_NOOP, OUTCOME_SUCCESS
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_not_due_when_no_templates(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        assert await job.is_due(db, org, datetime.date(2026, 7, 4)) is False


async def test_run_noop_writes_no_audit_and_no_notify(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    calls = {"audit": 0, "notify": 0}
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=0, settled=0),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter(calls, "audit"),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter(calls, "notify"),
    )
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        res = await job.run(db, org, datetime.date(2026, 7, 4))
    assert res.outcome == OUTCOME_NOOP
    assert calls == {"audit": 0, "notify": 0}


async def test_run_success_records_and_notifies(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    calls = {"audit": 0, "notify": 0}
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=2, settled=1),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter(calls, "audit", returns=42),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter(calls, "notify"),
    )
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        res = await job.run(db, org, datetime.date(2026, 7, 4))
    assert res.outcome == OUTCOME_SUCCESS
    assert res.counts == {"generated": 2, "settled": 1, "pending": 0}
    assert calls == {"audit": 1, "notify": 1}


def _fake_generate(*, generated, settled):
    async def _f(db, org_id):
        return {"generated": generated, "settled": settled, "pending": 0,
                "period_end": "2026-07-31"}
    return _f


def _counter(store, key, returns=None):
    async def _f(*a, **k):
        store[key] += 1
        return returns
    return _f
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_job_recurring.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement the job**

```python
# backend/app/services/scheduler/jobs/__init__.py
```

```python
# backend/app/services/scheduler/jobs/recurring_generation.py
from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring import RecurringTransaction
from app.models.user import Organization
from app.services.billing_service import current_cycle_window
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_recurring_generated
from app.services.recurring_service import generate_due_transactions
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.base import JobResult
from app.models.notification import NotificationCategory


class RecurringGenerationJob:
    job_type = "recurring_generation"
    setting_key = org_settings.AUTOMATE_RECURRING_KEY

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        _, period_end = current_cycle_window(org.billing_cycle_day, today)
        found = (
            await db.execute(
                select(RecurringTransaction.id).where(
                    RecurringTransaction.org_id == org.id,
                    RecurringTransaction.is_active == True,  # noqa: E712
                    RecurringTransaction.next_due_date <= period_end,
                ).limit(1)
            )
        ).first()
        return found is not None

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        result = await generate_due_transactions(db, org.id)
        await db.commit()
        generated = int(result.get("generated", 0))
        settled = int(result.get("settled", 0))
        counts = {"generated": generated, "settled": settled, "pending": int(result.get("pending", 0))}
        if generated == 0 and settled == 0:
            return JobResult.noop()
        audit_id = await record_run(job_type=self.job_type, outcome="success", org=org, detail=counts)
        title, body, link = scheduler_recurring_generated(generated=generated, settled=settled)
        await dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.org_activity,
            event_type=f"scheduler.{self.job_type}.success",
            title=title, body=body, link_url=link, audit_event_id=audit_id,
        )
        await db.commit()
        return JobResult.ok(counts)
```

Confirm `RecurringTransaction` has `is_active` and `next_due_date` (it does; see `recurring_service.py`). If the active flag has a different name, adjust the filter.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_job_recurring.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/jobs/ backend/tests/services/test_scheduler_job_recurring.py
git commit -m "feat(scheduler): recurring-generation job"
```

---

### Task 8: Billing-close job

**Files:**
- Create: `backend/app/services/scheduler/jobs/billing_close.py`
- Test: `backend/tests/services/test_scheduler_job_billing_close.py`

**Interfaces:**
- Consumes: `billing_service.get_current_period`, `billing_service.close_period`, `billing_service._snap_to_cycle`; `sched_audit.record_run`; members fan-out; `scheduler_billing_closed` template.
- Produces: `class BillingCloseJob` with `job_type="billing_close"`, `setting_key=org_settings.AUTOMATE_BILLING_KEY`, `is_due`, `run`.

**Due rule:** let `boundary = _snap_to_cycle(today, org.billing_cycle_day)` (the cycle_day on/before today). Due iff `get_current_period().start_date < boundary` (the open period straddles the boundary and has not been closed for it). **Close date** = `boundary - 1 day` so the new period opens exactly on `boundary`. Idempotent: after close, `current.start_date == boundary`, so next `is_due` is False.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_job_billing_close.py
from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services import billing_service
from app.services.scheduler.jobs.billing_close import BillingCloseJob


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(session_factory, cycle_day, period_start):
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=cycle_day)
        db.add(org); await db.flush()
        db.add(BillingPeriod(org_id=org.id, start_date=period_start))
        await db.commit(); await db.refresh(org)
        return org


async def test_not_due_before_boundary(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    async with session_factory() as db:
        # today is mid-cycle; boundary (Jul 1) == current start -> not due
        assert await job.is_due(db, org, datetime.date(2026, 7, 15)) is False


async def test_due_when_period_straddles_boundary(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    async with session_factory() as db:
        # today = Aug 3, boundary = Aug 1 > current start (Jul 1) -> due
        assert await job.is_due(db, org, datetime.date(2026, 8, 3)) is True


async def test_run_closes_and_is_idempotent(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    today = datetime.date(2026, 8, 3)
    async with session_factory() as db:
        res = await job.run(db, org, today)
    assert res.outcome == "success"
    # new open period starts on the boundary (Aug 1)
    async with session_factory() as db:
        cur = await billing_service.get_current_period(db, org.id)
        assert cur.start_date == datetime.date(2026, 8, 1)
        assert await job.is_due(db, org, today) is False  # idempotent


def _silence_side_effects(monkeypatch):
    async def _noop_audit(**k):
        return 1
    async def _noop_notify(*a, **k):
        return 0
    monkeypatch.setattr("app.services.scheduler.jobs.billing_close.record_run", _noop_audit)
    monkeypatch.setattr(
        "app.services.scheduler.jobs.billing_close.dispatch_notification_to_org_members", _noop_notify
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_job_billing_close.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement the job**

```python
# backend/app/services/scheduler/jobs/billing_close.py
from __future__ import annotations

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services import billing_service
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_billing_closed
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.base import JobResult


class BillingCloseJob:
    job_type = "billing_close"
    setting_key = org_settings.AUTOMATE_BILLING_KEY

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        boundary = billing_service._snap_to_cycle(today, org.billing_cycle_day)
        current = await billing_service.get_current_period(db, org.id)
        return current.start_date < boundary

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        boundary = billing_service._snap_to_cycle(today, org.billing_cycle_day)
        close_date = boundary - datetime.timedelta(days=1)
        new_period = await billing_service.close_period(db, org.id, close_date)
        await db.commit()
        counts = {
            "closed_on": close_date.isoformat(),
            "new_period_start": new_period.start_date.isoformat(),
        }
        audit_id = await record_run(job_type=self.job_type, outcome="success", org=org, detail=counts)
        title, body, link = scheduler_billing_closed(new_period_start=new_period.start_date)
        await dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.org_activity,
            event_type=f"scheduler.{self.job_type}.success",
            title=title, body=body, link_url=link, audit_event_id=audit_id,
        )
        await db.commit()
        return JobResult.ok(counts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_job_billing_close.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/jobs/billing_close.py backend/tests/services/test_scheduler_job_billing_close.py
git commit -m "feat(scheduler): billing-period auto-close job"
```

---

### Task 9: Billing pre-close reminder job

**Files:**
- Create: `backend/app/services/scheduler/jobs/billing_reminder.py`
- Test: `backend/tests/services/test_scheduler_job_billing_reminder.py`

**Interfaces:**
- Consumes: `billing_service.current_cycle_window` (to get `period_end`; next boundary = `period_end + 1 day`); `sched_audit.reminder_already_sent`, `sched_audit.record_reminder`; `org_settings.get_reminder_lead_days`; members fan-out; `scheduler_billing_close_reminder` template.
- Produces: `class BillingReminderJob` with `job_type="billing_close"` (shares the billing setting), `setting_key=org_settings.AUTOMATE_BILLING_KEY`, `is_due`, `run`.

**Due rule:** `next_boundary = period_end + 1 day`. `days_until = (next_boundary - today).days`. Due iff `0 < days_until <= lead_days` AND `not reminder_already_sent(next_boundary)`. `run` sends the members reminder and writes the `scheduler.billing_close.reminder` audit row (the dedup marker). Returns `JobResult.ok`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_job_billing_reminder.py
from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services.scheduler.jobs.billing_reminder import BillingReminderJob


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1)):
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=cycle_day)
        db.add(org); await db.flush()
        db.add(BillingPeriod(org_id=org.id, start_date=period_start))
        await db.commit(); await db.refresh(org)
        return org


async def test_due_within_lead_window(session_factory, monkeypatch):
    _stub(monkeypatch, already_sent=False, lead=3)
    org = await _seed(session_factory)
    job = BillingReminderJob()
    async with session_factory() as db:
        # next boundary Aug 1; today Jul 30 -> 2 days out, within lead=3
        assert await job.is_due(db, org, datetime.date(2026, 7, 30)) is True
        # today Jul 20 -> 12 days out, outside lead
        assert await job.is_due(db, org, datetime.date(2026, 7, 20)) is False


async def test_not_due_when_already_sent(session_factory, monkeypatch):
    _stub(monkeypatch, already_sent=True, lead=3)
    org = await _seed(session_factory)
    job = BillingReminderJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, datetime.date(2026, 7, 30)) is False


async def test_run_records_reminder_and_notifies(session_factory, monkeypatch):
    calls = {"reminder": 0, "notify": 0}
    _stub(monkeypatch, already_sent=False, lead=3, calls=calls)
    org = await _seed(session_factory)
    job = BillingReminderJob()
    async with session_factory() as db:
        res = await job.run(db, org, datetime.date(2026, 7, 30))
    assert res.outcome == "success"
    assert calls == {"reminder": 1, "notify": 1}


def _stub(monkeypatch, *, already_sent, lead, calls=None):
    calls = calls if calls is not None else {"reminder": 0, "notify": 0}
    async def _sent(db, org_id, period):
        return already_sent
    async def _lead(db, org_id):
        return lead
    async def _rec(**k):
        calls["reminder"] += 1; return 7
    async def _notify(*a, **k):
        calls["notify"] += 1; return 3
    monkeypatch.setattr("app.services.scheduler.jobs.billing_reminder.reminder_already_sent", _sent)
    monkeypatch.setattr("app.services.scheduler.jobs.billing_reminder.get_reminder_lead_days", _lead)
    monkeypatch.setattr("app.services.scheduler.jobs.billing_reminder.record_reminder", _rec)
    monkeypatch.setattr(
        "app.services.scheduler.jobs.billing_reminder.dispatch_notification_to_org_members", _notify
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_job_billing_reminder.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement the reminder job**

```python
# backend/app/services/scheduler/jobs/billing_reminder.py
from __future__ import annotations

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services.billing_service import current_cycle_window
from app.services.notification_service import dispatch_notification_to_org_members
from app.services.notification_templates import scheduler_billing_close_reminder
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_reminder, reminder_already_sent
from app.services.scheduler.base import JobResult
from app.services.scheduler.org_settings import get_reminder_lead_days


class BillingReminderJob:
    job_type = "billing_close"
    setting_key = org_settings.AUTOMATE_BILLING_KEY

    def _next_boundary(self, org: Organization, today: datetime.date) -> datetime.date:
        _, period_end = current_cycle_window(org.billing_cycle_day, today)
        return period_end + datetime.timedelta(days=1)

    async def is_due(self, db: AsyncSession, org: Organization, today: datetime.date) -> bool:
        boundary = self._next_boundary(org, today)
        days_until = (boundary - today).days
        lead = await get_reminder_lead_days(db, org.id)
        if not (0 < days_until <= lead):
            return False
        return not await reminder_already_sent(db, org.id, boundary)

    async def run(self, db: AsyncSession, org: Organization, today: datetime.date) -> JobResult:
        boundary = self._next_boundary(org, today)
        days_until = (boundary - today).days
        audit_id = await record_reminder(org=org, period_start=boundary, detail={"days_until": days_until})
        title, body, link = scheduler_billing_close_reminder(close_date=boundary, days_until=days_until)
        await dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.org_activity,
            event_type="scheduler.billing_close.reminder",
            title=title, body=body, link_url=link, audit_event_id=audit_id,
        )
        await db.commit()
        return JobResult.ok({"period_start": boundary.isoformat(), "days_until": days_until})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_job_billing_reminder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/jobs/billing_reminder.py backend/tests/services/test_scheduler_job_billing_reminder.py
git commit -m "feat(scheduler): billing pre-close reminder job"
```

---

### Task 10: Runner (per-org/per-job execution with failure isolation)

**Files:**
- Create: `backend/app/services/scheduler/runner.py`
- Test: `backend/tests/services/test_scheduler_runner.py`

**Interfaces:**
- Consumes: `async_session` factory; `Organization`; the three jobs; `org_settings.get_bool`; `sched_audit.record_run`.
- Produces:
  - `REGISTRY: list[ScheduledJob]` = `[RecurringGenerationJob(), BillingReminderJob(), BillingCloseJob()]`.
  - `async def run_all_due(today: datetime.date, *, session_factory=async_session, registry=REGISTRY) -> None`.

**Runner contract:** for each org, for each job: open a fresh session; skip if the job's `setting_key` is disabled; `try` `is_due` → `run`; on ANY exception, roll back and write a `scheduler.<job_type>.failure` audit row (via `record_run`), then continue to the next job/org. One failure never aborts the sweep. Emit structlog `scheduler.job.*` events.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_runner.py
from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler import runner as R
from app.services.scheduler.base import JobResult


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class _Job:
    def __init__(self, job_type, setting_key, *, due=True, boom=False, log=None):
        self.job_type = job_type; self.setting_key = setting_key
        self._due = due; self._boom = boom; self._log = log if log is not None else []
    async def is_due(self, db, org, today): return self._due
    async def run(self, db, org, today):
        if self._boom: raise RuntimeError("kaboom")
        self._log.append(("ran", self.job_type)); return JobResult.ok({})


async def test_disabled_job_is_skipped(session_factory, monkeypatch):
    log = []
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return key != "off_key"
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    async with session_factory() as db:
        db.add(Organization(name="A", billing_cycle_day=1)); await db.commit()
    reg = [_Job("j_on", "on_key", log=log), _Job("j_off", "off_key", log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory, registry=reg)
    assert ("ran", "j_on") in log
    assert ("ran", "j_off") not in log


async def test_one_job_failure_does_not_abort_sweep(session_factory, monkeypatch):
    log = []
    failures = {"n": 0}
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return True
    async def _fail_audit(**k):
        if k.get("outcome") == "failure": failures["n"] += 1
        return 1
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    monkeypatch.setattr(R, "record_run", _fail_audit)
    async with session_factory() as db:
        db.add(Organization(name="A", billing_cycle_day=1)); await db.commit()
    reg = [_Job("boom", "k1", boom=True, log=log), _Job("good", "k2", log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory, registry=reg)
    assert ("ran", "good") in log     # ran despite the earlier failure
    assert failures["n"] == 1          # failure was audited
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_runner.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement the runner**

```python
# backend/app/services/scheduler/runner.py
from __future__ import annotations

import datetime

import structlog
from sqlalchemy import select

from app.database import async_session
from app.models.user import Organization
from app.services.scheduler import org_settings
from app.services.scheduler.audit import record_run
from app.services.scheduler.jobs.billing_close import BillingCloseJob
from app.services.scheduler.jobs.billing_reminder import BillingReminderJob
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob

logger = structlog.get_logger(__name__)

REGISTRY = [RecurringGenerationJob(), BillingReminderJob(), BillingCloseJob()]


async def run_all_due(today: datetime.date, *, session_factory=async_session, registry=REGISTRY) -> None:
    async with session_factory() as db:
        orgs = (await db.execute(select(Organization))).scalars().all()
    for org in orgs:
        for job in registry:
            async with session_factory() as db:
                try:
                    if not await org_settings.get_bool(db, org.id, job.setting_key):
                        continue
                    if not await job.is_due(db, org, today):
                        continue
                    result = await job.run(db, org, today)
                    await logger.ainfo("scheduler.job.%s" % result.outcome,
                                       job=job.job_type, org_id=org.id, counts=result.counts)
                except Exception as exc:  # noqa: BLE001 — isolate per-job failures
                    await db.rollback()
                    await record_run(job_type=job.job_type, outcome="failure", org=org,
                                     detail={"error": str(exc)})
                    await logger.aerror("scheduler.job.failure", job=job.job_type,
                                        org_id=org.id, error=str(exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_runner.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/runner.py backend/tests/services/test_scheduler_runner.py
git commit -m "feat(scheduler): per-org runner with failure isolation"
```

---

### Task 11: Scheduler loop + Redis single-runner lock

**Files:**
- Create: `backend/app/services/scheduler/loop.py`
- Test: `backend/tests/services/test_scheduler_loop.py`

**Interfaces:**
- Consumes: `redis_client.get_client()` (returns `Redis | None`); `runner.run_all_due`.
- Produces:
  - `LOCK_KEY = "scheduler:tick:lock"`
  - `async def acquire_tick_lock(ttl_seconds: int) -> bool` (None client in dev → returns True).
  - `async def run_one_tick(today, lock_ttl) -> bool` (acquires lock, runs sweep if acquired; returns whether it ran).
  - `async def scheduler_loop(stop_event, *, tick_seconds, lock_ttl) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_scheduler_loop.py
# conftest.py autouses a fake redis (see tests/conftest.py::_autouse_fake_redis),
# so get_client() returns a working fake here.
import asyncio
import datetime
import pytest

from app.services.scheduler import loop as L


async def test_tick_runs_when_lock_acquired(monkeypatch):
    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    did = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert did is True
    assert ran["n"] == 1


async def test_second_tick_skipped_while_lock_held(monkeypatch):
    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    first = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    second = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert first is True and second is False
    assert ran["n"] == 1  # lock still held -> second sweep skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_loop.py -v`
Expected: FAIL (import error).

- [ ] **Step 3: Implement the loop**

```python
# backend/app/services/scheduler/loop.py
from __future__ import annotations

import asyncio
import datetime

import structlog

from app import redis_client
from app.services.scheduler.runner import run_all_due

logger = structlog.get_logger(__name__)

LOCK_KEY = "scheduler:tick:lock"


async def acquire_tick_lock(ttl_seconds: int) -> bool:
    client = redis_client.get_client()
    if client is None:
        # Dev / no-redis: single process, no contention to guard against.
        return True
    got = await client.set(LOCK_KEY, "1", nx=True, ex=ttl_seconds)
    return bool(got)


async def run_one_tick(today: datetime.date, *, lock_ttl: int) -> bool:
    if not await acquire_tick_lock(lock_ttl):
        await logger.ainfo("scheduler.tick.skip_locked")
        return False
    await logger.ainfo("scheduler.tick.start")
    await run_all_due(today)
    await logger.ainfo("scheduler.tick.complete")
    return True


async def scheduler_loop(stop_event: asyncio.Event, *, tick_seconds: int, lock_ttl: int) -> None:
    while not stop_event.is_set():
        try:
            await run_one_tick(datetime.date.today(), lock_ttl=lock_ttl)
        except Exception as exc:  # noqa: BLE001 — never let the ticker die
            await logger.aerror("scheduler.tick.error", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/services/test_scheduler_loop.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduler/loop.py backend/tests/services/test_scheduler_loop.py
git commit -m "feat(scheduler): tick loop with Redis single-runner lock"
```

---

### Task 12: Wire the loop into the FastAPI lifespan

**Files:**
- Modify: `backend/app/main.py` (lifespan)
- Test: `backend/tests/test_scheduler_lifespan.py`

**Interfaces:**
- Consumes: `app_settings.scheduler_enabled / scheduler_tick_seconds / scheduler_lock_ttl_seconds`; `scheduler_loop`.
- Produces: on startup (when enabled) an `asyncio.Task` stored at `app.state.scheduler_task` plus `app.state.scheduler_stop`; on shutdown the event is set and the task awaited/cancelled.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_scheduler_lifespan.py
import asyncio
import pytest
from app import main as app_main


async def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    started = {"n": 0}
    async def _fake_loop(stop_event, *, tick_seconds, lock_ttl):
        started["n"] += 1
        await stop_event.wait()  # block until shutdown sets the event
    monkeypatch.setattr(app_main, "scheduler_loop", _fake_loop)
    monkeypatch.setattr(app_main.app_settings, "scheduler_enabled", True)
    # Skip dev migrations for this test.
    monkeypatch.setattr(app_main.app_settings, "app_env", "production")

    async with app_main.lifespan(app_main.app):
        # inside the context the task should be running
        assert started["n"] == 1
        assert not app_main.app.state.scheduler_task.done()
    # after exit the task is finished
    assert app_main.app.state.scheduler_task.done()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec backend pytest tests/test_scheduler_lifespan.py -v`
Expected: FAIL (`scheduler_loop` not imported in main / no `app.state.scheduler_task`).

- [ ] **Step 3: Wire the lifespan**

At the top of `backend/app/main.py` add the import:

```python
from app.services.scheduler.loop import scheduler_loop
```

Inside `lifespan`, after the `starting` log and BEFORE `yield`:

```python
    if app_settings.scheduler_enabled:
        app.state.scheduler_stop = asyncio.Event()
        app.state.scheduler_task = asyncio.create_task(
            scheduler_loop(
                app.state.scheduler_stop,
                tick_seconds=app_settings.scheduler_tick_seconds,
                lock_ttl=app_settings.scheduler_lock_ttl_seconds,
            )
        )
```

After `yield`, BEFORE `await redis_client.close_client()`:

```python
    if app_settings.scheduler_enabled and getattr(app.state, "scheduler_task", None):
        app.state.scheduler_stop.set()
        try:
            await asyncio.wait_for(app.state.scheduler_task, timeout=10)
        except asyncio.TimeoutError:
            app.state.scheduler_task.cancel()
```

(Confirm `import asyncio` is present at the top of `main.py`; add it if missing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-sched exec backend pytest tests/test_scheduler_lifespan.py -v`
Expected: PASS.

- [ ] **Step 5: Full backend scheduler suite + commit**

```bash
docker compose -p team-sched exec backend pytest tests/ -k scheduler -v
git add backend/app/main.py backend/tests/test_scheduler_lifespan.py
git commit -m "feat(scheduler): start/stop the tick loop in the app lifespan"
```

---

### Task 13: Frontend org settings toggles

**Files:**
- Modify: `frontend/lib/api.ts` (typed getter/setter) and `frontend/lib/types.ts` (types)
- Modify: `frontend/app/settings/organization/page.tsx` (add a "Automatic tasks" card)
- Test: `frontend/tests/scheduler-settings.test.tsx` (mirror an existing settings-card test)

**Interfaces:**
- Consumes: `GET/PUT /api/v1/scheduler/settings` from Task 3.
- Produces: `SchedulerSettings` type; `getSchedulerSettings()` / `updateSchedulerSettings(patch)` in `api.ts`; a settings card with three controls (two switches + a number input) using the shared design tokens/components (`frontend/lib/styles.ts`); no raw Tailwind palette colors (design-token CI gate).

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/scheduler-settings.test.tsx
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import SchedulerSettingsCard from "@/components/settings/SchedulerSettingsCard";
import * as api from "@/lib/api";

beforeEach(() => {
  vi.spyOn(api, "getSchedulerSettings").mockResolvedValue({
    automate_recurring_generation: true,
    automate_billing_close: true,
    billing_close_reminder_lead_days: 3,
  });
  vi.spyOn(api, "updateSchedulerSettings").mockResolvedValue({
    automate_recurring_generation: true,
    automate_billing_close: false,
    billing_close_reminder_lead_days: 3,
  });
});

describe("SchedulerSettingsCard", () => {
  it("loads and renders current settings", async () => {
    render(<SchedulerSettingsCard />);
    await waitFor(() => expect(screen.getByText(/Automatic tasks/i)).toBeInTheDocument());
    expect(screen.getByLabelText(/Automatically close billing period/i)).toBeChecked();
  });

  it("persists a toggle change", async () => {
    render(<SchedulerSettingsCard />);
    await waitFor(() => screen.getByLabelText(/Automatically close billing period/i));
    fireEvent.click(screen.getByLabelText(/Automatically close billing period/i));
    await waitFor(() =>
      expect(api.updateSchedulerSettings).toHaveBeenCalledWith({ automate_billing_close: false })
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-sched exec frontend npm test -- tests/scheduler-settings.test.tsx`
Expected: FAIL (component + api functions do not exist).

- [ ] **Step 3: Implement types, api, component, and mount**

Add to `frontend/lib/types.ts`:

```ts
export interface SchedulerSettings {
  automate_recurring_generation: boolean;
  automate_billing_close: boolean;
  billing_close_reminder_lead_days: number;
}
```

Add to `frontend/lib/api.ts` (using the file's existing fetch wrapper):

```ts
export async function getSchedulerSettings(): Promise<SchedulerSettings> {
  return apiFetch<SchedulerSettings>("/api/v1/scheduler/settings");
}

export async function updateSchedulerSettings(
  patch: Partial<SchedulerSettings>,
): Promise<SchedulerSettings> {
  return apiFetch<SchedulerSettings>("/api/v1/scheduler/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}
```

Create `frontend/components/settings/SchedulerSettingsCard.tsx` — a card titled "Automatic tasks" with:
- a labeled switch "Automatically generate recurring transactions" bound to `automate_recurring_generation`,
- a labeled switch "Automatically close billing period" bound to `automate_billing_close`,
- a number input "Days before close to notify members" bound to `billing_close_reminder_lead_days` (min 0, max 31),
each calling `updateSchedulerSettings` with only the changed field on change; load via `getSchedulerSettings` in a `useEffect` seeding local `useState` (do NOT re-seed state from a prop in an effect; see the prop-state-reset flake note in team memory). Use existing card/switch primitives from `frontend/lib/styles.ts`; no raw palette colors.

Mount `<SchedulerSettingsCard />` in `frontend/app/settings/organization/page.tsx` alongside the other org settings cards (admin-only section, matching how the existing org-admin cards there are gated).

- [ ] **Step 4: Run tests + type-check + lint**

Run:
```bash
docker compose -p team-sched exec frontend npm test -- tests/scheduler-settings.test.tsx
docker compose -p team-sched exec frontend npx tsc --noEmit
docker compose -p team-sched exec frontend npx eslint . --quiet
docker compose -p team-sched exec frontend bash scripts/check-design-tokens.sh
```
Expected: tests PASS, tsc clean, eslint clean, design-token check clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts frontend/components/settings/SchedulerSettingsCard.tsx frontend/app/settings/organization/page.tsx frontend/tests/scheduler-settings.test.tsx
git commit -m "feat(scheduler): org settings toggles UI"
```

---

## Final verification (before PR)

- [ ] Backend: `docker compose -p team-sched exec backend pytest tests/ -k scheduler -v` (all green).
- [ ] Backend full suite unaffected: `docker compose -p team-sched exec backend pytest tests/services tests/routers -q`.
- [ ] Frontend: full `npm test` run (not just the touched file), `tsc --noEmit`, `eslint . --quiet`, design-token check.
- [ ] Manual smoke (verify skill): with the stack up, PUT `/api/v1/scheduler/settings` toggling billing off, confirm `/admin/audit` filtered by `scheduler.billing_close.success` shows a row after a forced tick, and that a member with `org_activity` off receives no notification.
- [ ] Open the PR per the team workflow (branch already `feat/scheduled-tasks-subsystem`; conventional-commit title `feat(scheduler): automatic scheduled tasks (recurring + billing close)`), then auto-dispatch the review team before merge.

## Notes for the implementer

- CC-bill close is intentionally OUT of scope (deferred to v2). The registry is built so adding a third job later is a drop-in.
- The reminder job and the close job both key off `AUTOMATE_BILLING_KEY` on purpose: turning billing automation off silences both the heads-up and the close.
- No migration in this plan. If any test needs a schema you thought was missing, re-check: `OrgSetting`, `audit_events`, notifications, `billing_periods`, `recurring_transactions` all already exist.
- Idempotency is load-bearing. Never make a job that mutates without a domain-state `is_due` guard that flips false after the mutation.
- Multi-period catch-up is intentionally COLLAPSED: `BillingCloseJob` closes to the most recent `_snap_to_cycle(today)` boundary only. If the app was down across two or more billing boundaries, the intervening periods merge into a single close at the latest boundary rather than replaying one close per missed boundary. This is acceptable for v1 (downtime spanning multiple billing months is an extreme case). If per-boundary replay is ever wanted, loop the close from `current.start_date` forward inside `run`. Do not "fix" the current behavior silently.
- The router admin guard (Task 3) and several router/notification test fixtures (`client`, `admin_auth_headers`, `member_auth_headers`, `org_with_members`) are referenced by the convention of the nearest existing test in `tests/routers/` and `tests/services/`. Before writing those tests, open one sibling test file and copy its exact fixture names and seed helpers rather than assuming these.
