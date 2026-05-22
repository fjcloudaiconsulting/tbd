"""Router tests for the notification substrate (specs
``2026-05-21-notification-system-sensitive-ops.md`` +
``2026-05-22-notification-system-2nd-arch-pass.md``).

Pins the architect-locked invariants at the HTTP layer:

- Auth gate: anonymous requests are 401 across every endpoint.
- Cross-user isolation: PATCH on another user's row returns 404.
- POST /mark-seen sets ``seen_at`` for all unseen rows; leaves
  ``read_at`` alone.
- PATCH /{id} marks a single row read; idempotent.
- GET /preferences auto-creates the row with the locked defaults.
- PUT /preferences happy path round-trips.
- PUT /preferences with ``email_security=false`` returns 400 with
  body shape ``{detail: {code: "security_emails_required", ...}}``
  — the route-level check, NOT a Pydantic-validator 422.
- Cursor pagination: 3 pages, every row appears exactly once.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

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

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.notification import (
    Notification,
    NotificationCategory,
    UserNotificationPreferences,
)
from app.models.user import Organization, Role, User
from app.routers.notifications import router as notifications_router
from app.security import hash_password
from app.services import notification_service


# ── fixtures ──────────────────────────────────────────────────────


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
            try:
                yield session
                await session.commit()
            finally:
                await session.close()

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(notifications_router)
    return app


async def _seed_users(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        alice = User(
            org_id=org.id,
            username="alice",
            email="alice@ex.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        bob = User(
            org_id=org.id,
            username="bob",
            email="bob@ex.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        db.add_all([alice, bob])
        await db.commit()
        return {"org_id": org.id, "alice_id": alice.id, "bob_id": bob.id}


def _user_resolver(username: str):
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one()
    return resolve


# ── auth gate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_requires_auth(session_factory):
    """Anonymous request fails. We don't override get_current_user
    here, so FastAPI's HTTPBearer dependency raises before reaching
    the handler. TestClient surfaces this as 403 (no Authorization
    header) under HTTPBearer's default; the gate is the same — no
    anonymous access. The contract: every notifications endpoint is
    authed."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(notifications_router)
    with TestClient(app) as client:
        res = client.get("/api/v1/notifications")
    # HTTPBearer's default behaviour: missing credentials -> 403.
    # Either 401 or 403 satisfies "no anonymous access".
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_mark_seen_requires_auth(session_factory):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(notifications_router)
    with TestClient(app) as client:
        res = client.post("/api/v1/notifications/mark-seen")
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_preferences_get_requires_auth(session_factory):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(notifications_router)
    with TestClient(app) as client:
        res = client.get("/api/v1/notifications/preferences")
    assert res.status_code in (401, 403)


# ── list ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_empty(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.get("/api/v1/notifications")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_returns_rows_newest_first(session_factory):
    seeds = await _seed_users(session_factory)
    async with session_factory() as db:
        for i in range(3):
            await notification_service.dispatch_notification(
                db,
                user_id=seeds["alice_id"],
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
        await db.commit()

    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.get("/api/v1/notifications")
    assert res.status_code == 200
    body = res.json()
    titles = [row["title"] for row in body["items"]]
    assert titles == ["T2", "T1", "T0"]
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_cursor_pagination_three_pages_no_overlap(session_factory):
    """Three pages of fixture data, asserts:
    - exactly 3 pages walked
    - next_cursor is None on the last page
    - no row appears in two consecutive pages
    - every id is covered exactly once
    """
    seeds = await _seed_users(session_factory)
    ids: list[int] = []
    async with session_factory() as db:
        for i in range(7):
            row = await notification_service.dispatch_notification(
                db,
                user_id=seeds["alice_id"],
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
            ids.append(row.id)
        await db.commit()

    seen: set[int] = set()
    pages: list[list[int]] = []
    cursor: str | None = None
    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        for _ in range(5):  # guard cap
            params = {"limit": 3}
            if cursor is not None:
                params["cursor"] = cursor
            res = client.get("/api/v1/notifications", params=params)
            assert res.status_code == 200
            body = res.json()
            page_ids = [row["id"] for row in body["items"]]
            assert seen.isdisjoint(page_ids), (
                f"cursor pagination produced overlap on page {len(pages)}: "
                f"already-seen {seen & set(page_ids)}"
            )
            seen.update(page_ids)
            pages.append(page_ids)
            cursor = body["next_cursor"]
            if cursor is None:
                break

    assert len(pages) == 3
    assert [len(p) for p in pages] == [3, 3, 1]
    assert seen == set(ids)


@pytest.mark.asyncio
async def test_list_only_returns_current_user_rows(session_factory):
    seeds = await _seed_users(session_factory)
    async with session_factory() as db:
        await notification_service.dispatch_notification(
            db,
            user_id=seeds["alice_id"],
            category=NotificationCategory.SECURITY,
            event_type="e.a",
            title="A",
            body="A",
        )
        await notification_service.dispatch_notification(
            db,
            user_id=seeds["bob_id"],
            category=NotificationCategory.SECURITY,
            event_type="e.b",
            title="B",
            body="B",
        )
        await db.commit()

    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.get("/api/v1/notifications")
    body = res.json()
    titles = [row["title"] for row in body["items"]]
    assert titles == ["A"]


@pytest.mark.asyncio
async def test_list_malformed_cursor_returns_400(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/notifications", params={"cursor": "not-a-cursor"}
        )
    assert res.status_code == 400
    body = res.json()
    assert body["detail"]["code"] == "invalid_cursor"


# ── mark-seen ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_seen_clears_unseen_leaves_read_alone(session_factory):
    seeds = await _seed_users(session_factory)
    async with session_factory() as db:
        for i in range(3):
            row = await notification_service.dispatch_notification(
                db,
                user_id=seeds["alice_id"],
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
            if i == 0:
                # Pre-mark one as read so we can verify mark_seen
                # doesn't touch read_at.
                row.read_at = row.created_at
        await db.commit()

    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.post("/api/v1/notifications/mark-seen")
    assert res.status_code == 204

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification)
                .where(Notification.user_id == seeds["alice_id"])
                .order_by(Notification.id)
            )
        ).scalars().all()
    assert all(r.seen_at is not None for r in rows)
    # The pre-read row's read_at is still set; the other two are
    # still unread.
    assert rows[0].read_at is not None
    assert rows[1].read_at is None
    assert rows[2].read_at is None


@pytest.mark.asyncio
async def test_mark_seen_does_not_touch_other_users(session_factory):
    seeds = await _seed_users(session_factory)
    async with session_factory() as db:
        await notification_service.dispatch_notification(
            db,
            user_id=seeds["bob_id"],
            category=NotificationCategory.SECURITY,
            event_type="e.b",
            title="B",
            body="B",
        )
        await db.commit()

    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.post("/api/v1/notifications/mark-seen")
    assert res.status_code == 204

    async with session_factory() as db:
        bob_row = (
            await db.execute(
                select(Notification).where(
                    Notification.user_id == seeds["bob_id"]
                )
            )
        ).scalar_one()
    assert bob_row.seen_at is None


# ── mark-read ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_read_sets_only_targeted(session_factory):
    seeds = await _seed_users(session_factory)
    ids: list[int] = []
    async with session_factory() as db:
        for i in range(3):
            row = await notification_service.dispatch_notification(
                db,
                user_id=seeds["alice_id"],
                category=NotificationCategory.SECURITY,
                event_type=f"e.{i}",
                title=f"T{i}",
                body=f"B{i}",
            )
            ids.append(row.id)
        await db.commit()

    target = ids[1]
    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.patch(f"/api/v1/notifications/{target}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == target
    assert body["read_at"] is not None

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Notification)
                .where(Notification.user_id == seeds["alice_id"])
                .order_by(Notification.id)
            )
        ).scalars().all()
    states = [(r.id, r.read_at is not None) for r in rows]
    assert states == [
        (ids[0], False),
        (ids[1], True),
        (ids[2], False),
    ]


@pytest.mark.asyncio
async def test_mark_read_cross_user_returns_404(session_factory):
    seeds = await _seed_users(session_factory)
    async with session_factory() as db:
        row = await notification_service.dispatch_notification(
            db,
            user_id=seeds["bob_id"],
            category=NotificationCategory.SECURITY,
            event_type="e",
            title="T",
            body="B",
        )
        await db.commit()
        nid = row.id

    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.patch(f"/api/v1/notifications/{nid}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_mark_read_missing_returns_404(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))
    with TestClient(app) as client:
        res = client.patch("/api/v1/notifications/9999")
    assert res.status_code == 404


# ── preferences GET (auto-create) ─────────────────────────────────


@pytest.mark.asyncio
async def test_preferences_get_auto_creates_with_defaults(session_factory):
    seeds = await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))

    # Confirm no row exists before the GET.
    async with session_factory() as db:
        existing = (
            await db.execute(
                select(UserNotificationPreferences).where(
                    UserNotificationPreferences.user_id == seeds["alice_id"]
                )
            )
        ).scalar_one_or_none()
    assert existing is None

    with TestClient(app) as client:
        res = client.get("/api/v1/notifications/preferences")
    assert res.status_code == 200
    body = res.json()
    assert body["email_security"] is True
    assert body["email_account"] is True
    assert body["email_org_admin"] is True
    assert body["email_org_activity"] is False
    assert body["in_app_security"] is True
    assert body["in_app_account"] is True
    assert body["in_app_org_admin"] is True
    assert body["in_app_org_activity"] is False

    # The row now exists.
    async with session_factory() as db:
        persisted = (
            await db.execute(
                select(UserNotificationPreferences).where(
                    UserNotificationPreferences.user_id == seeds["alice_id"]
                )
            )
        ).scalar_one()
    assert persisted.user_id == seeds["alice_id"]


# ── preferences PUT (happy path + 400) ────────────────────────────


def _full_payload(**overrides) -> dict:
    base = {
        "email_security": True,
        "email_account": True,
        "email_org_admin": True,
        "email_org_activity": False,
        "in_app_security": True,
        "in_app_account": True,
        "in_app_org_admin": True,
        "in_app_org_activity": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_preferences_put_happy_path(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))
    payload = _full_payload(
        email_account=False,
        email_org_admin=False,
        email_org_activity=True,
        in_app_account=False,
        in_app_org_activity=True,
    )
    with TestClient(app) as client:
        res = client.put("/api/v1/notifications/preferences", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["email_security"] is True
    assert body["email_account"] is False
    assert body["email_org_admin"] is False
    assert body["email_org_activity"] is True
    assert body["in_app_security"] is True
    assert body["in_app_account"] is False
    assert body["in_app_org_admin"] is True
    assert body["in_app_org_activity"] is True


@pytest.mark.asyncio
async def test_preferences_put_rejects_email_security_false_with_400(
    session_factory,
):
    """The architect-locked envelope shape — pinned exactly.

    Must be a 400 (not a 422), and the detail must be a dict with
    ``code`` + ``message`` keys (NOT a list-of-errors shape, which
    is what FastAPI's default request-validation handler would
    produce if the check were a Pydantic field validator).
    """
    await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))
    payload = _full_payload(email_security=False)
    with TestClient(app) as client:
        res = client.put("/api/v1/notifications/preferences", json=payload)
    assert res.status_code == 400
    body = res.json()
    # Envelope shape: detail must be an object, not a list.
    assert isinstance(body["detail"], dict), (
        f"expected route-level 400 envelope, got {body!r}"
    )
    assert body["detail"]["code"] == "security_emails_required"
    assert "message" in body["detail"]
    assert body["detail"]["message"]  # non-empty


@pytest.mark.asyncio
async def test_preferences_put_round_trip_via_get(session_factory):
    await _seed_users(session_factory)
    app = _make_app(session_factory, _user_resolver("alice"))
    payload = _full_payload(email_account=False, email_org_admin=False)
    with TestClient(app) as client:
        put_res = client.put("/api/v1/notifications/preferences", json=payload)
        assert put_res.status_code == 200
        get_res = client.get("/api/v1/notifications/preferences")
    assert get_res.status_code == 200
    assert get_res.json()["email_account"] is False
    assert get_res.json()["email_org_admin"] is False
