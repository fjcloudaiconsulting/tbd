"""Service-layer tests for the notification substrate.

Pins the architect-locked invariants exercised at the function level
(without the FastAPI request/commit envelope):

- ``dispatch_notification`` writes the row, optionally carrying
  ``link_url`` and ``audit_event_id``.
- ``mark_seen`` clears ``seen_at`` for every unseen row and leaves
  ``read_at`` untouched.
- ``mark_read`` sets ``read_at`` only on the targeted row; a second
  call is a no-op.
- ``list_for_user`` honors cursor pagination and never repeats a
  row across consecutive pages.
- ``get_preferences`` auto-creates the row with the locked defaults.
- ``update_preferences`` round-trips the writable fields and
  defense-in-depth-forces ``email_security=True`` on write.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.notification import (
    Notification,
    NotificationCategory,
    UserNotificationPreferences,
)
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import notification_service


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


async def _seed_user(factory, *, username: str = "alice", email: str = "alice@ex.io") -> int:
    async with factory() as db:
        org = Organization(name=f"Org-{username}", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


# ── dispatch_notification ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_notification_happy_path_minimal(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.SECURITY,
            event_type="user.password.changed",
            title="Your password was changed",
            body="A change happened.",
        )
        await db.commit()
    assert row.id is not None
    assert row.user_id == user_id
    assert row.category == NotificationCategory.SECURITY
    assert row.event_type == "user.password.changed"
    assert row.title == "Your password was changed"
    assert row.body == "A change happened."
    assert row.link_url is None
    assert row.audit_event_id is None
    assert row.read_at is None
    assert row.seen_at is None
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_dispatch_notification_with_link_url(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.ACCOUNT,
            event_type="account.role_changed",
            title="Role changed",
            body="Your role was updated.",
            link_url="/settings/profile",
        )
        await db.commit()
    assert row.link_url == "/settings/profile"


@pytest.mark.asyncio
async def test_dispatch_notification_with_audit_event_id(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        audit_row = AuditEvent(
            event_type="user.password.changed",
            actor_email="alice@ex.io",
            outcome=AuditOutcome.SUCCESS,
        )
        db.add(audit_row)
        await db.commit()
        await db.refresh(audit_row)
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.SECURITY,
            event_type="user.password.changed",
            title="Password changed",
            body="Your password was changed.",
            audit_event_id=audit_row.id,
        )
        await db.commit()
    assert row.audit_event_id == audit_row.id


@pytest.mark.asyncio
async def test_dispatch_notification_with_link_and_audit(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        audit_row = AuditEvent(
            event_type="admin.org.plan.changed",
            actor_email="admin@ex.io",
            outcome=AuditOutcome.SUCCESS,
        )
        db.add(audit_row)
        await db.commit()
        await db.refresh(audit_row)
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.ORG_ADMIN,
            event_type="admin.org.plan.changed",
            title="Plan changed",
            body="Org plan was changed.",
            link_url="/settings/billing",
            audit_event_id=audit_row.id,
        )
        await db.commit()
    assert row.link_url == "/settings/billing"
    assert row.audit_event_id == audit_row.id


# ── mark_seen ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_seen_clears_all_unseen_only(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        for i in range(3):
            await notification_service.dispatch_notification(
                db,
                user_id=user_id,
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
        await db.commit()

    async with session_factory() as db:
        touched = await notification_service.mark_seen(db, user_id=user_id)
        await db.commit()
    assert touched == 3

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification).where(Notification.user_id == user_id)
            )
        ).scalars().all()
    assert all(r.seen_at is not None for r in rows)
    # read_at must not have been touched.
    assert all(r.read_at is None for r in rows)

    # A second call returns 0 — idempotent.
    async with session_factory() as db:
        touched = await notification_service.mark_seen(db, user_id=user_id)
        await db.commit()
    assert touched == 0


@pytest.mark.asyncio
async def test_mark_seen_does_not_clear_other_users(session_factory):
    alice = await _seed_user(session_factory, username="alice", email="a@ex.io")
    bob = await _seed_user(session_factory, username="bob", email="b@ex.io")
    async with session_factory() as db:
        for u in (alice, bob):
            await notification_service.dispatch_notification(
                db,
                user_id=u,
                category=NotificationCategory.SECURITY,
                event_type="e",
                title="t",
                body="b",
            )
        await db.commit()

    async with session_factory() as db:
        await notification_service.mark_seen(db, user_id=alice)
        await db.commit()

    async with session_factory() as db:
        bob_row = (
            await db.execute(
                select(Notification).where(Notification.user_id == bob)
            )
        ).scalar_one()
    assert bob_row.seen_at is None


# ── mark_read ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_read_sets_only_targeted_row(session_factory):
    user_id = await _seed_user(session_factory)
    ids: list[int] = []
    async with session_factory() as db:
        for i in range(3):
            row = await notification_service.dispatch_notification(
                db,
                user_id=user_id,
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
            ids.append(row.id)
        await db.commit()
    target_id = ids[1]

    async with session_factory() as db:
        updated = await notification_service.mark_read(
            db, user_id=user_id, notification_id=target_id
        )
        await db.commit()
    assert updated is not None
    assert updated.read_at is not None

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.id)
            )
        ).scalars().all()
    read_states = [(r.id, r.read_at is not None) for r in rows]
    assert read_states == [
        (ids[0], False),
        (ids[1], True),
        (ids[2], False),
    ]


@pytest.mark.asyncio
async def test_mark_read_idempotent_preserves_timestamp(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.SECURITY,
            event_type="e",
            title="t",
            body="b",
        )
        await db.commit()
        nid = row.id

    async with session_factory() as db:
        updated = await notification_service.mark_read(
            db, user_id=user_id, notification_id=nid
        )
        await db.commit()
    first_read = updated.read_at
    assert first_read is not None

    async with session_factory() as db:
        again = await notification_service.mark_read(
            db, user_id=user_id, notification_id=nid
        )
        await db.commit()
    assert again is not None
    # Idempotent: original timestamp preserved.
    assert again.read_at == first_read


@pytest.mark.asyncio
async def test_mark_read_cross_user_returns_none(session_factory):
    alice = await _seed_user(session_factory, username="alice", email="a@ex.io")
    bob = await _seed_user(session_factory, username="bob", email="b@ex.io")
    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=alice,
            category=NotificationCategory.SECURITY,
            event_type="e",
            title="t",
            body="b",
        )
        await db.commit()
        nid = row.id

    async with session_factory() as db:
        updated = await notification_service.mark_read(
            db, user_id=bob, notification_id=nid
        )
    assert updated is None


# ── list_for_user (cursor pagination) ─────────────────────────────


@pytest.mark.asyncio
async def test_list_for_user_cursor_pagination_three_pages(session_factory):
    user_id = await _seed_user(session_factory)
    # Seed 7 rows so a limit=3 walk gives 3 pages (3 + 3 + 1).
    ids: list[int] = []
    async with session_factory() as db:
        for i in range(7):
            row = await notification_service.dispatch_notification(
                db,
                user_id=user_id,
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
            ids.append(row.id)
        await db.commit()

    seen_ids: set[int] = set()
    pages: list[list[int]] = []
    cursor: str | None = None
    async with session_factory() as db:
        for _ in range(5):  # guard cap; we expect exactly 3 iterations
            page = await notification_service.list_for_user(
                db, user_id=user_id, cursor=cursor, limit=3
            )
            page_ids = [r.id for r in page.items]
            pages.append(page_ids)
            # No row appears in two consecutive pages.
            assert seen_ids.isdisjoint(page_ids), (
                "cursor pagination produced an overlapping row"
            )
            seen_ids.update(page_ids)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

    assert len(pages) == 3
    assert len(pages[0]) == 3
    assert len(pages[1]) == 3
    assert len(pages[2]) == 1
    # Newest first: highest id should be on page 0.
    assert pages[0][0] == ids[-1]
    # All 7 ids covered, exactly once.
    assert seen_ids == set(ids)


@pytest.mark.asyncio
async def test_list_for_user_empty_returns_no_cursor(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        page = await notification_service.list_for_user(
            db, user_id=user_id, cursor=None, limit=10
        )
    assert page.items == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_list_for_user_invalid_cursor_raises(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        with pytest.raises(ValueError):
            await notification_service.list_for_user(
                db, user_id=user_id, cursor="not_a_cursor", limit=10
            )


# ── get_preferences (auto-create) ─────────────────────────────────


@pytest.mark.asyncio
async def test_get_preferences_auto_creates_row_with_defaults(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        existing = (
            await db.execute(
                select(UserNotificationPreferences).where(
                    UserNotificationPreferences.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    assert existing is None

    async with session_factory() as db:
        prefs = await notification_service.get_preferences(db, user_id=user_id)
        await db.commit()
    assert prefs.user_id == user_id
    # Default values per architect lock.
    assert prefs.email_security is True
    assert prefs.email_account is True
    assert prefs.email_org_admin is True
    assert prefs.email_org_activity is False
    assert prefs.in_app_security is True
    assert prefs.in_app_account is True
    assert prefs.in_app_org_admin is True
    assert prefs.in_app_org_activity is False

    # Second call returns the same row.
    async with session_factory() as db:
        prefs2 = await notification_service.get_preferences(db, user_id=user_id)
    assert prefs2.user_id == user_id


# ── update_preferences ────────────────────────────────────────────


class _PrefPayload:
    """Light stand-in matching the schema attribute names; the
    service only reads attributes so we don't import the Pydantic
    class here to keep this test independent of the wire layer."""

    def __init__(self, **kwargs):
        self.email_security = kwargs.get("email_security", True)
        self.email_account = kwargs.get("email_account", True)
        self.email_org_admin = kwargs.get("email_org_admin", True)
        self.email_org_activity = kwargs.get("email_org_activity", False)
        self.in_app_security = kwargs.get("in_app_security", True)
        self.in_app_account = kwargs.get("in_app_account", True)
        self.in_app_org_admin = kwargs.get("in_app_org_admin", True)
        self.in_app_org_activity = kwargs.get("in_app_org_activity", False)


@pytest.mark.asyncio
async def test_update_preferences_round_trips(session_factory):
    user_id = await _seed_user(session_factory)
    payload = _PrefPayload(
        email_security=True,
        email_account=False,
        email_org_admin=False,
        email_org_activity=True,
        in_app_security=True,
        in_app_account=False,
        in_app_org_admin=True,
        in_app_org_activity=True,
    )
    async with session_factory() as db:
        prefs = await notification_service.update_preferences(
            db, user_id=user_id, payload=payload
        )
        await db.commit()
    assert prefs.email_security is True
    assert prefs.email_account is False
    assert prefs.email_org_admin is False
    assert prefs.email_org_activity is True
    assert prefs.in_app_security is True
    assert prefs.in_app_account is False
    assert prefs.in_app_org_admin is True
    assert prefs.in_app_org_activity is True


@pytest.mark.asyncio
async def test_update_preferences_forces_security_true_defense_in_depth(session_factory):
    """The route layer is the real gate for email_security=False, but
    the service force-coerces to True as defense in depth so an
    internal call site that forgets the route check cannot persist a
    broken state.
    """
    user_id = await _seed_user(session_factory)
    payload = _PrefPayload(email_security=False)
    async with session_factory() as db:
        prefs = await notification_service.update_preferences(
            db, user_id=user_id, payload=payload
        )
        await db.commit()
    assert prefs.email_security is True
