"""Router tests for the announcement banner substrate (spec
2026-05-21).

Pins the architect-locked invariants:

- Customer GET filters by active flag, schedule window, and
  per-user dismissals (with maintenance force-shown).
- Severity-then-newest ordering of the customer list.
- POST /dismiss is idempotent and 400-rejects maintenance severity
  with the structured ``code=announcement_not_dismissible`` body.
- Admin CRUD requires superadmin (403 for a regular member).
- Admin create/update/delete each write an ``audit_events`` row
  on an independent session.
- end_at <= start_at returns 422 on both create and update paths.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.announcement import (
    Announcement,
    AnnouncementSeverity,
    UserDismissedAnnouncement,
)
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.routers.admin_announcements import router as admin_router
from app.routers.announcements import router as user_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(user_router)
    app.include_router(admin_router)
    return app


async def _seed_users(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Platform", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        sa = User(
            org_id=org.id,
            username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=org.id,
            username="user",
            email="u@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {"org_id": org.id, "sa_id": sa.id, "plain_id": plain.id}


def _superadmin_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


def _plain_user_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(False)))
            ).scalar_one()
    return resolve


async def _seed_announcement(
    factory,
    *,
    title: str = "Hello",
    body: str = "Body",
    severity: AnnouncementSeverity = AnnouncementSeverity.INFO,
    is_active: bool = True,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> int:
    async with factory() as db:
        row = Announcement(
            title=title,
            body=body,
            severity=severity,
            is_active=is_active,
            start_at=start_at,
            end_at=end_at,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


# ── admin auth gate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_list_requires_superadmin(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/announcements")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_create_requires_superadmin(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/announcements",
            json={"title": "T", "body": "B", "severity": "info"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_delete_requires_superadmin(session_factory):
    await _seed_users(session_factory)
    ann_id = await _seed_announcement(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/announcements/{ann_id}")
    assert res.status_code == 403


# ── admin CRUD happy path + audit ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_writes_audit_event(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/announcements",
            json={
                "title": "Maintenance window",
                "body": "We are doing maintenance.",
                "severity": "maintenance",
                "is_active": True,
            },
        )
    assert res.status_code == 201
    body = res.json()
    assert body["title"] == "Maintenance window"
    assert body["severity"] == "maintenance"
    assert body["created_by_user_id"] is not None

    async with session_factory() as db:
        events = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "system.announcement.created"
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].detail["severity"] == "maintenance"


@pytest.mark.asyncio
async def test_update_writes_audit_event_and_changes_fields(session_factory):
    await _seed_users(session_factory)
    ann_id = await _seed_announcement(session_factory, title="Old")
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={"title": "New title", "is_active": False},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "New title"
    assert body["is_active"] is False

    async with session_factory() as db:
        events = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "system.announcement.updated"
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert sorted(events[0].detail["patched_fields"]) == ["is_active", "title"]


@pytest.mark.asyncio
async def test_delete_writes_audit_event_and_cascades_dismissals(session_factory):
    seeds = await _seed_users(session_factory)
    ann_id = await _seed_announcement(session_factory)
    async with session_factory() as db:
        db.add(
            UserDismissedAnnouncement(
                user_id=seeds["plain_id"], announcement_id=ann_id
            )
        )
        await db.commit()
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/announcements/{ann_id}")
    assert res.status_code == 204

    async with session_factory() as db:
        remaining_ann = (
            await db.execute(select(Announcement).where(Announcement.id == ann_id))
        ).scalar_one_or_none()
        remaining_dismiss = (
            await db.execute(
                select(UserDismissedAnnouncement).where(
                    UserDismissedAnnouncement.announcement_id == ann_id
                )
            )
        ).all()
        events = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "system.announcement.deleted"
                )
            )
        ).scalars().all()
    assert remaining_ann is None
    assert remaining_dismiss == []
    assert len(events) == 1


# ── schedule validation (end_at > start_at) ────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_end_before_start(session_factory):
    await _seed_users(session_factory)
    now = utcnow_naive()
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/announcements",
            json={
                "title": "Bad window",
                "body": "Body",
                "severity": "info",
                "start_at": (now + timedelta(hours=2)).isoformat(),
                "end_at": (now + timedelta(hours=1)).isoformat(),
            },
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_equal_start_end(session_factory):
    await _seed_users(session_factory)
    now = utcnow_naive()
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/announcements",
            json={
                "title": "Equal window",
                "body": "Body",
                "severity": "info",
                "start_at": now.isoformat(),
                "end_at": now.isoformat(),
            },
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_update_rejects_end_before_existing_start(session_factory):
    await _seed_users(session_factory)
    now = utcnow_naive()
    ann_id = await _seed_announcement(
        session_factory,
        start_at=now + timedelta(hours=5),
    )
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        # PATCH only end_at to be BEFORE the existing start_at — the
        # per-payload validator can't catch it, the router must.
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={"end_at": (now + timedelta(hours=1)).isoformat()},
        )
    assert res.status_code == 422


# ── customer-facing list filtering + ordering ──────────────────────────


@pytest.mark.asyncio
async def test_list_excludes_inactive(session_factory):
    await _seed_users(session_factory)
    await _seed_announcement(session_factory, title="off", is_active=False)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/announcements")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_list_excludes_future_and_expired(session_factory):
    await _seed_users(session_factory)
    now = utcnow_naive()
    await _seed_announcement(
        session_factory,
        title="future",
        start_at=now + timedelta(hours=1),
    )
    await _seed_announcement(
        session_factory,
        title="expired",
        end_at=now - timedelta(hours=1),
    )
    await _seed_announcement(session_factory, title="active")
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/announcements")
    titles = [row["title"] for row in res.json()]
    assert titles == ["active"]


@pytest.mark.asyncio
async def test_list_orders_by_severity_then_recent(session_factory):
    await _seed_users(session_factory)
    # Insert in a deliberate "wrong" order; the route must reorder.
    await _seed_announcement(session_factory, title="i1", severity=AnnouncementSeverity.INFO)
    await _seed_announcement(session_factory, title="m1", severity=AnnouncementSeverity.MAINTENANCE)
    await _seed_announcement(session_factory, title="p1", severity=AnnouncementSeverity.PROMO)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/announcements")
    titles = [row["title"] for row in res.json()]
    # maintenance first, then promo, then info — created_at desc within.
    assert titles == ["m1", "p1", "i1"]


@pytest.mark.asyncio
async def test_list_hides_dismissed_for_non_maintenance(session_factory):
    seeds = await _seed_users(session_factory)
    info_id = await _seed_announcement(
        session_factory, title="info", severity=AnnouncementSeverity.INFO
    )
    maint_id = await _seed_announcement(
        session_factory, title="maint", severity=AnnouncementSeverity.MAINTENANCE
    )
    async with session_factory() as db:
        # Dismiss BOTH; only the maintenance row should remain visible.
        db.add_all([
            UserDismissedAnnouncement(user_id=seeds["plain_id"], announcement_id=info_id),
            UserDismissedAnnouncement(user_id=seeds["plain_id"], announcement_id=maint_id),
        ])
        await db.commit()
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/announcements")
    titles = [row["title"] for row in res.json()]
    assert titles == ["maint"]


# ── dismissal behaviour ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dismiss_is_idempotent(session_factory):
    seeds = await _seed_users(session_factory)
    ann_id = await _seed_announcement(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        r1 = client.post(f"/api/v1/announcements/{ann_id}/dismiss")
        r2 = client.post(f"/api/v1/announcements/{ann_id}/dismiss")
    assert r1.status_code == 204
    assert r2.status_code == 204
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(UserDismissedAnnouncement).where(
                    UserDismissedAnnouncement.user_id == seeds["plain_id"]
                )
            )
        ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_dismiss_maintenance_returns_400(session_factory):
    await _seed_users(session_factory)
    ann_id = await _seed_announcement(
        session_factory, severity=AnnouncementSeverity.MAINTENANCE
    )
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.post(f"/api/v1/announcements/{ann_id}/dismiss")
    assert res.status_code == 400
    body = res.json()
    assert body["detail"]["code"] == "announcement_not_dismissible"


@pytest.mark.asyncio
async def test_dismiss_unknown_id_returns_404(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.post("/api/v1/announcements/99999/dismiss")
    assert res.status_code == 404


# ── PATCH null-rejection on non-nullable fields ────────────────────────
#
# Architect-locked PR #340 review (2026-05-22): explicit ``null`` on a
# non-nullable column must return a deterministic 422 with a
# field-level error BEFORE any DB write. The first revision relied on
# SQLAlchemy / enum-coerce blowing up at flush time, which surfaced as
# a generic 500 with no actionable diagnostic. ``start_at`` and
# ``end_at`` are both legitimately nullable on the row — the admin
# form sends them as ``null`` when the optional schedule fields are
# left blank, and that payload must round-trip cleanly. Only
# ``title``, ``body``, ``severity``, and ``is_active`` are rejected.


@pytest.mark.parametrize("field", ["title", "body", "severity", "is_active"])
@pytest.mark.asyncio
async def test_update_rejects_explicit_null_for_non_nullable_field(
    session_factory, field
):
    await _seed_users(session_factory)
    ann_id = await _seed_announcement(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={field: None},
        )
    assert res.status_code == 422, res.text
    body = res.json()
    # FastAPI surfaces Pydantic ValueError as a structured 422 — the
    # error message MUST identify which field failed so the admin UI
    # can route the message to the right form input.
    detail = body.get("detail")
    assert detail is not None
    detail_blob = str(detail).lower()
    assert field in detail_blob, detail


@pytest.mark.asyncio
async def test_update_accepts_null_end_at_clearing_schedule(session_factory):
    """``end_at`` is legitimately nullable on PATCH — operators must
    be able to clear a previously-set end via
    ``PATCH {"end_at": null}``.
    """
    await _seed_users(session_factory)
    now = utcnow_naive()
    ann_id = await _seed_announcement(
        session_factory,
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=10),
    )
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={"end_at": None},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["end_at"] is None


@pytest.mark.asyncio
async def test_update_accepts_null_start_at_clearing_schedule(session_factory):
    """``start_at`` is legitimately nullable on PATCH — operators must
    be able to clear a previously-set start via
    ``PATCH {"start_at": null}``. The admin form depends on this:
    when the optional Start field is left blank, the form sends
    ``start_at: null`` for the patch payload (see
    ``frontend/app/system/announcements/page.tsx:fromDatetimeLocal``).
    """
    await _seed_users(session_factory)
    now = utcnow_naive()
    ann_id = await _seed_announcement(
        session_factory,
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=10),
    )
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={"start_at": None},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["start_at"] is None


@pytest.mark.asyncio
async def test_update_accepts_null_for_both_schedule_endpoints(session_factory):
    """The admin form's edit flow for an unbounded announcement sends
    ``{ start_at: null, end_at: null, ... }`` — both keys must be
    accepted in the SAME patch payload, clearing both columns to NULL
    in one round-trip. Pins
    ``frontend/app/system/announcements/page.tsx`` against silent
    422s on the edit form.
    """
    await _seed_users(session_factory)
    now = utcnow_naive()
    ann_id = await _seed_announcement(
        session_factory,
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=10),
    )
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={
                "title": "Updated",
                "body": "Updated body",
                "severity": "info",
                "is_active": True,
                "start_at": None,
                "end_at": None,
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["start_at"] is None
    assert body["end_at"] is None
    assert body["title"] == "Updated"


@pytest.mark.asyncio
async def test_update_with_empty_body_is_noop(session_factory):
    """An empty PATCH body MUST NOT mutate any field. (Key-missing
    semantics: the schema sees nothing, the router setattr loop runs
    zero times.)
    """
    await _seed_users(session_factory)
    ann_id = await _seed_announcement(
        session_factory, title="Original", body="Original body"
    )
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(f"/api/v1/admin/announcements/{ann_id}", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["title"] == "Original"
    assert body["body"] == "Original body"


@pytest.mark.asyncio
async def test_update_null_error_is_returned_before_db_write(session_factory):
    """A PATCH that sends ``title: null`` MUST 422 without mutating
    the row (no setattr/commit). This pins the "validation before DB"
    contract the architect locked in.
    """
    await _seed_users(session_factory)
    ann_id = await _seed_announcement(session_factory, title="Untouched")
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/announcements/{ann_id}",
            json={"title": None, "body": "would-be-applied"},
        )
    assert res.status_code == 422, res.text

    # Re-read the row and confirm nothing changed.
    async with session_factory() as db:
        fresh = await db.get(Announcement, ann_id)
        assert fresh is not None
        assert fresh.title == "Untouched"
        assert fresh.body != "would-be-applied"
