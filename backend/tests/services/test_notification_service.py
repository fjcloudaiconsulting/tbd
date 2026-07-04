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

import ast
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
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


@pytest.fixture
def _structlog_via_stdlib():
    """Ensure structlog routes events through stdlib for the test.

    The notification fanout tests assert on structured fields emitted
    by :mod:`app.services.notification_service`. The service uses
    ``structlog.stdlib.get_logger()``; whether ``caplog`` sees those
    events depends on whether structlog has been configured to route
    through the stdlib pipeline. ``app.logging.setup_logging`` installs
    that wiring in production and in the FastAPI lifespan, but the
    bare unit-test conftest does not — so structlog falls back to its
    default ``PrintLogger`` which bypasses stdlib entirely.

    This fixture calls ``setup_logging()`` once per test so events
    land in ``caplog`` regardless of test ordering. Without it, the
    test passes only when an earlier test in the session happened to
    initialise the FastAPI app stack first; that ordering is what made
    the original ``structlog.testing.capture_logs()`` form pass locally
    and fail in CI.
    """
    import structlog

    from app.logging import setup_logging

    original_config = structlog.get_config() if structlog.is_configured() else None
    setup_logging()
    yield
    if original_config is not None:
        structlog.configure(**original_config)


def _collect_structlog_events(caplog) -> list[dict]:
    """Pull structlog events out of pytest's ``caplog`` capture.

    With ``_structlog_via_stdlib`` active, structlog is configured to
    end its processor chain with ``ProcessorFormatter.wrap_for_formatter``
    which hands the event dict to stdlib. ``caplog`` then sees a
    :class:`logging.LogRecord` whose ``msg`` is either the event dict
    itself (when ``wrap_for_formatter`` ran) or a string the formatter
    chain rendered. This helper normalises both shapes into a list of
    event ``dict``s so assertions can reach the structured fields.
    """
    events: list[dict] = []
    for rec in caplog.records:
        # Path 1: wrap_for_formatter — record.msg is the event dict OR a
        # tuple of (event_dict,). Older structlog versions ship the dict
        # straight through; newer versions pass it as the first positional.
        candidate = rec.msg
        if isinstance(candidate, tuple) and candidate:
            candidate = candidate[0]
        if isinstance(candidate, dict):
            events.append(candidate)
            continue
        # Path 2: rendered to text — try JSON first, then fall back to a
        # Python-literal eval for repr-style dict strings.
        message = rec.getMessage()
        for parser in (json.loads, ast.literal_eval):
            try:
                payload = parser(message)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(payload, dict):
                events.append(payload)
                break
    return events


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
    assert prefs.email_org_activity is True
    assert prefs.in_app_security is True
    assert prefs.in_app_account is True
    assert prefs.in_app_org_admin is True
    assert prefs.in_app_org_activity is True

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


# ── PR3: preference-aware dispatch + org-admin fanout ─────────────


async def _seed_extra_admin(
    factory, org_id: int, *, username: str, email: str, role
) -> int:
    """Add a second user with ``role`` to an existing org. Returns id."""
    from app.security import hash_password as _hp  # local import — test scope

    async with factory() as db:
        user = User(
            org_id=org_id,
            username=username,
            email=email,
            password_hash=_hp("pw-1234567"),
            role=role,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


@pytest.mark.asyncio
async def test_dispatch_respects_in_app_preference(session_factory):
    """When the user has ``in_app_account=False``, an ACCOUNT category
    dispatch must NOT write a row. Locks the preference-aware
    behaviour added in PR3 — without it the bell would surface rows
    the user explicitly opted out of.
    """
    user_id = await _seed_user(session_factory)
    # Persist a preference row with account turned off.
    payload = _PrefPayload(in_app_account=False)
    async with session_factory() as db:
        await notification_service.update_preferences(
            db, user_id=user_id, payload=payload
        )
        await db.commit()

    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.ACCOUNT,
            event_type="account.role_changed",
            title="Role changed",
            body="Body",
        )
        await db.commit()
    # Skipped → returns None and no row exists.
    assert row is None
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification).where(Notification.user_id == user_id)
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_dispatch_force_writes_for_security_category(session_factory):
    """Even with every in_app_* preference flipped off, a SECURITY
    dispatch still writes the row. Architect-locked force-on rule —
    the user cannot opt out of security signals in the inbox.
    """
    user_id = await _seed_user(session_factory)
    payload = _PrefPayload(
        in_app_security=False,
        in_app_account=False,
        in_app_org_admin=False,
        in_app_org_activity=False,
    )
    async with session_factory() as db:
        await notification_service.update_preferences(
            db, user_id=user_id, payload=payload
        )
        await db.commit()

    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.SECURITY,
            event_type="user.password.changed",
            title="Your password was changed",
            body="Body",
        )
        await db.commit()
    assert row is not None
    assert row.category == NotificationCategory.SECURITY


@pytest.mark.asyncio
async def test_dispatch_org_admin_fanout_to_multiple_admins(session_factory):
    """3-admin org → 3 dispatched rows. Pins the architect-locked
    fanout behavior: a single SELECT pulls the admin set, then per-user
    rows are written. A spy on the session counts the SELECTs against
    ``users`` to prove the helper doesn't N+1.
    """
    # Seed the org + 1st owner via the standard helper.
    seed_user_id = await _seed_user(
        session_factory, username="owner", email="owner@ex.io"
    )

    # Need the owner's org_id to add siblings.
    async with session_factory() as db:
        owner = await db.get(User, seed_user_id)
        assert owner is not None
        org_id = owner.org_id

    # Add two more admins (role=ADMIN) — total 3 admins in the org.
    admin_a = await _seed_extra_admin(
        session_factory, org_id, username="admin_a", email="a@ex.io", role=Role.ADMIN
    )
    admin_b = await _seed_extra_admin(
        session_factory, org_id, username="admin_b", email="b@ex.io", role=Role.ADMIN
    )

    # And a MEMBER who must NOT receive the broadcast.
    member_id = await _seed_extra_admin(
        session_factory,
        org_id,
        username="member",
        email="m@ex.io",
        role=Role.MEMBER,
    )

    select_counts = {"users": 0}
    async with session_factory() as db:
        # Patch the session's execute to count SELECTs against users.
        original_execute = db.execute

        async def counting_execute(clause, *args, **kwargs):
            text = str(clause).lower()
            if "from users" in text:
                select_counts["users"] += 1
            return await original_execute(clause, *args, **kwargs)

        db.execute = counting_execute  # type: ignore[method-assign]

        written = await notification_service.dispatch_notification_to_org_admins(
            db,
            org_id=org_id,
            category=NotificationCategory.ORG_ADMIN,
            event_type="admin.org.plan.changed",
            title="Plan changed",
            body="Body",
        )
        await db.commit()

    assert written == 3
    # One SELECT against users for the admin lookup. Even if other
    # SELECTs run from prior context, the helper itself must add
    # exactly one — assert "at most" to absorb any session bookkeeping.
    assert select_counts["users"] == 1

    # All three admins got a row; the member did not.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    recipient_ids = {row.user_id for row in rows}
    assert recipient_ids == {seed_user_id, admin_a, admin_b}
    assert member_id not in recipient_ids


@pytest.mark.asyncio
async def test_dispatch_org_admin_fanout_continues_on_individual_failure(
    session_factory, monkeypatch
):
    """One admin's dispatch raises → the other two still get their
    rows. Locks the best-effort contract: a poison-pill row write
    cannot poison the broadcast.
    """
    seed_user_id = await _seed_user(
        session_factory, username="owner", email="owner@ex.io"
    )
    async with session_factory() as db:
        owner = await db.get(User, seed_user_id)
        org_id = owner.org_id

    admin_a = await _seed_extra_admin(
        session_factory, org_id, username="admin_a", email="a@ex.io", role=Role.ADMIN
    )
    admin_b = await _seed_extra_admin(
        session_factory, org_id, username="admin_b", email="b@ex.io", role=Role.ADMIN
    )

    real_dispatch = notification_service.dispatch_notification

    async def flaky_dispatch(db, *, user_id, **kwargs):
        if user_id == admin_a:
            raise RuntimeError("simulated per-user failure")
        return await real_dispatch(db, user_id=user_id, **kwargs)

    monkeypatch.setattr(
        notification_service, "dispatch_notification", flaky_dispatch
    )

    async with session_factory() as db:
        written = await notification_service.dispatch_notification_to_org_admins(
            db,
            org_id=org_id,
            category=NotificationCategory.ORG_ADMIN,
            event_type="admin.org.plan.changed",
            title="Plan changed",
            body="Body",
        )
        await db.commit()

    # 2 written: owner + admin_b. admin_a raised and was skipped.
    assert written == 2
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    recipient_ids = {row.user_id for row in rows}
    assert seed_user_id in recipient_ids
    assert admin_b in recipient_ids
    assert admin_a not in recipient_ids


@pytest.mark.asyncio
async def test_fanout_savepoint_isolates_recipient_flush_failure(
    session_factory, monkeypatch, caplog, _structlog_via_stdlib
):
    """A flush-time IntegrityError on recipient #2 must NOT poison
    the outer session: recipient #1 (already flushed) and recipient #3
    (after the SAVEPOINT rollback) both keep their rows, and the
    caller's commit succeeds.

    Without ``db.begin_nested()`` per recipient, a failed flush leaves
    the session in "rollback-required" state — recipient #3's
    subsequent ``db.flush()`` would raise ``PendingRollbackError`` and
    the eventual ``db.commit()`` would fail too, even though the loop
    swallows the per-user exception.
    """
    seed_user_id = await _seed_user(
        session_factory, username="owner", email="owner@ex.io"
    )
    async with session_factory() as db:
        owner = await db.get(User, seed_user_id)
        org_id = owner.org_id

    admin_a = await _seed_extra_admin(
        session_factory, org_id, username="admin_a", email="a@ex.io", role=Role.ADMIN
    )
    admin_b = await _seed_extra_admin(
        session_factory, org_id, username="admin_b", email="b@ex.io", role=Role.ADMIN
    )

    real_dispatch = notification_service.dispatch_notification

    async def poisoning_dispatch(db, *, user_id, **kwargs):
        if user_id == admin_a:
            # Force a real flush-time failure: insert a Notification
            # with a user_id that violates the FK. SQLite has
            # PRAGMA foreign_keys=ON in our fixture so this raises
            # IntegrityError on flush, leaving the session in
            # rollback-required state. This is the contract we need
            # the savepoint to isolate.
            from app.models.notification import Notification
            from app._time import utcnow_naive

            bad = Notification(
                user_id=999_999,  # no such user
                category=kwargs["category"],
                event_type=kwargs["event_type"],
                title=kwargs["title"],
                body=kwargs["body"],
                created_at=utcnow_naive(),
            )
            db.add(bad)
            await db.flush()  # raises IntegrityError
            return bad  # unreachable
        return await real_dispatch(db, user_id=user_id, **kwargs)

    monkeypatch.setattr(
        notification_service, "dispatch_notification", poisoning_dispatch
    )

    with caplog.at_level(logging.WARNING, logger="app.services.notification_service"):
        async with session_factory() as db:
            written = await notification_service.dispatch_notification_to_org_admins(
                db,
                org_id=org_id,
                category=NotificationCategory.ORG_ADMIN,
                event_type="admin.org.plan.changed",
                title="Plan changed",
                body="Body",
            )
            # CRITICAL: the outer commit must succeed. Without the
            # savepoint wrap this raises PendingRollbackError because
            # recipient #2's flush poisoned the session.
            await db.commit()

    # 2 written: owner + admin_b. admin_a's flush failed and the
    # savepoint rolled back cleanly.
    assert written == 2

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    recipient_ids = {row.user_id for row in rows}
    assert seed_user_id in recipient_ids
    assert admin_b in recipient_ids
    assert admin_a not in recipient_ids
    # The FK-violating row from the poisoned dispatch must not have
    # leaked past the savepoint rollback.
    assert 999_999 not in recipient_ids

    captured_events = _collect_structlog_events(caplog)
    failed_events = [
        ev
        for ev in captured_events
        if ev.get("event") == "notification.dispatch.fanout.recipient_failed"
    ]
    assert len(failed_events) == 1
    failed = failed_events[0]
    assert failed["recipient_user_id"] == admin_a
    assert failed["org_id"] == org_id
    assert failed["error_class"] == "IntegrityError"


@pytest.mark.asyncio
async def test_fanout_returns_success_and_failure_counts(
    session_factory, monkeypatch, caplog, _structlog_via_stdlib
):
    """Mix 2 successes + 2 failures: the helper returns the success
    count (rows actually written) and emits a structured completion
    log carrying both ``rows_written`` and ``failures``.

    This pins the observable shape callers (admin_orgs.py) depend on:
    return is the integer success count; failure count is reachable
    via structlog for ops dashboards.
    """
    seed_user_id = await _seed_user(
        session_factory, username="owner", email="owner@ex.io"
    )
    async with session_factory() as db:
        owner = await db.get(User, seed_user_id)
        org_id = owner.org_id

    # Three more admins → 4 total (owner + 3 admins).
    admin_a = await _seed_extra_admin(
        session_factory, org_id, username="admin_a", email="a@ex.io", role=Role.ADMIN
    )
    admin_b = await _seed_extra_admin(
        session_factory, org_id, username="admin_b", email="b@ex.io", role=Role.ADMIN
    )
    admin_c = await _seed_extra_admin(
        session_factory, org_id, username="admin_c", email="c@ex.io", role=Role.ADMIN
    )

    real_dispatch = notification_service.dispatch_notification
    failing_ids = {admin_a, admin_c}

    async def half_failing_dispatch(db, *, user_id, **kwargs):
        if user_id in failing_ids:
            raise RuntimeError(f"simulated dispatch failure for {user_id}")
        return await real_dispatch(db, user_id=user_id, **kwargs)

    monkeypatch.setattr(
        notification_service, "dispatch_notification", half_failing_dispatch
    )

    with caplog.at_level(logging.INFO, logger="app.services.notification_service"):
        async with session_factory() as db:
            written = await notification_service.dispatch_notification_to_org_admins(
                db,
                org_id=org_id,
                category=NotificationCategory.ORG_ADMIN,
                event_type="admin.org.plan.changed",
                title="Plan changed",
                body="Body",
            )
            await db.commit()

    # 2 successes (owner + admin_b); 2 failures (admin_a + admin_c).
    assert written == 2

    captured_events = _collect_structlog_events(caplog)
    complete_events = [
        ev
        for ev in captured_events
        if ev.get("event") == "notification.dispatch.fanout.complete"
    ]
    assert len(complete_events) == 1
    complete = complete_events[0]
    assert complete["org_id"] == org_id
    assert complete["admin_count"] == 4
    assert complete["rows_written"] == 2
    assert complete["failures"] == 2

    failed_events = [
        ev
        for ev in captured_events
        if ev.get("event") == "notification.dispatch.fanout.recipient_failed"
    ]
    assert {ev["recipient_user_id"] for ev in failed_events} == failing_ids

    # Verify only the 2 successful rows actually landed.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    assert {row.user_id for row in rows} == {seed_user_id, admin_b}
