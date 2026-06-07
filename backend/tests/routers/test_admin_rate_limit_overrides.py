"""Router tests for the rate-limit override CRUD (L4.10).

Pins the architect-locked invariants:

- Every endpoint is superadmin-only. A regular member, an org admin,
  and a member of a different org all 403.
- Create / update / delete each write an ``audit_events`` row with
  ``event_type=admin.rate_limit.{created,updated,deleted}`` on an
  independent session.
- ``max_requests=0`` returns 422 from the schema (self-lockout
  guard).
- Both org and user scope on the same payload returns 422
  (exactly-one-of).
- Override with no scope returns 422.
- Update endpoint cannot change scope (scope fields not in update
  schema).
- A non-existent id returns 404.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.rate_limit_override import RateLimitOverride
from app.models.user import Organization, User
from app.routers.admin_rate_limit_overrides import router as admin_router


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


@pytest.fixture(autouse=True)
def stub_redis(monkeypatch):
    """No-op Redis for the router path so cache calls don't trip."""
    import app.redis_client as rc

    monkeypatch.setattr(rc, "get_client", lambda: None)


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
    app.include_router(admin_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        superadmin = User(
            org_id=org.id,
            username="sa",
            email="sa@example.com",
            password_hash="x",
            role="owner",
            is_superadmin=True,
        )
        org_admin = User(
            org_id=org.id,
            username="oa",
            email="oa@example.com",
            password_hash="x",
            role="admin",
            is_superadmin=False,
        )
        member = User(
            org_id=org.id,
            username="m",
            email="m@example.com",
            password_hash="x",
            role="member",
            is_superadmin=False,
        )
        db.add_all([superadmin, org_admin, member])
        await db.commit()
        for u in (superadmin, org_admin, member):
            await db.refresh(u)
        return {
            "org_id": org.id,
            "superadmin_id": superadmin.id,
            "org_admin_id": org_admin.id,
            "member_id": member.id,
        }


def _resolver_for(seeded: dict, who: str):
    async def _r(factory):
        async with factory() as db:
            uid = {
                "superadmin": seeded["superadmin_id"],
                "org_admin": seeded["org_admin_id"],
                "member": seeded["member_id"],
            }[who]
            return await db.get(User, uid)

    return _r


@pytest.mark.asyncio
async def test_list_requires_superadmin(session_factory):
    seeded = await _seed(session_factory)
    for who in ("org_admin", "member"):
        app = _make_app(session_factory, _resolver_for(seeded, who))
        client = TestClient(app)
        resp = client.get("/api/v1/admin/rate-limit-overrides")
        assert resp.status_code == 403, who


@pytest.mark.asyncio
async def test_create_requires_superadmin(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "org_admin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_happy_path_writes_audit(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
            "note": "B2B customer ramp",
        },
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["org_id"] == seeded["org_id"]
    assert payload["user_id"] is None
    assert payload["max_requests"] == 100
    assert payload["created_by_user_id"] == seeded["superadmin_id"]

    # Audit row written.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.rate_limit.created"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_user_id == seeded["superadmin_id"]
    assert rows[0].target_org_id == seeded["org_id"]
    assert rows[0].outcome.value == "success"
    assert rows[0].detail["max_requests"] == 100


@pytest.mark.asyncio
async def test_create_rejects_both_scopes(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "user_id": seeded["member_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_no_scope(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_zero_max_requests(session_factory):
    """Self-lockout guard: 0 requests/minute would brick the
    superadmin who set it. The schema's MAX_REQUESTS_MIN=1 fences
    this off.
    """
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 0,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_past_expiry(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    past = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
            "expires_at": past,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_happy_path_writes_audit(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 201, resp.text
    override_id = resp.json()["id"]
    resp = client.patch(
        f"/api/v1/admin/rate-limit-overrides/{override_id}",
        json={"max_requests": 250},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_requests"] == 250

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.rate_limit.updated"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert "max_requests" in rows[0].detail["patched_fields"]


@pytest.mark.asyncio
async def test_update_404_for_missing(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.patch(
        "/api/v1/admin/rate-limit-overrides/9999",
        json={"max_requests": 250},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_rejects_scope_change(session_factory):
    """The update schema does not surface ``org_id`` / ``user_id``,
    so a payload trying to move scope is rejected as
    ``extra forbidden``.
    """
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    override_id = resp.json()["id"]
    resp = client.patch(
        f"/api/v1/admin/rate-limit-overrides/{override_id}",
        json={"org_id": 9999},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_writes_audit(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    override_id = resp.json()["id"]
    resp = client.delete(
        f"/api/v1/admin/rate-limit-overrides/{override_id}"
    )
    assert resp.status_code == 204
    # Row is gone.
    async with session_factory() as db:
        remaining = (
            await db.execute(select(RateLimitOverride))
        ).scalars().all()
    assert remaining == []
    # Audit recorded.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.rate_limit.deleted"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["override_id"] == override_id


@pytest.mark.asyncio
async def test_list_filters_by_org(session_factory):
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "user_id": seeded["member_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 5,
            "period_seconds": 60,
        },
    )
    resp = client.get(
        f"/api/v1/admin/rate-limit-overrides?org_id={seeded['org_id']}"
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["org_id"] == seeded["org_id"]


@pytest.mark.asyncio
async def test_create_rejects_unknown_endpoint_pattern(session_factory):
    """An override referencing a pattern not in either catalogue set
    is 422'd before it ever reaches the DB. The error body includes
    the typed ``endpoint_pattern_unknown`` code and the full
    overridable catalogue so the caller can recover without a
    separate round-trip.
    """
    from app.rate_limit_endpoint_catalogue import (
        sorted_overridable_patterns,
    )

    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "fake.endpoint",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.text
    # Typed error code lets the UI branch on shape, not string match.
    assert "endpoint_pattern_unknown" in body
    # And the typo'd value is echoed back so the operator sees what
    # was rejected.
    assert "fake.endpoint" in body
    # Every OVERRIDABLE catalogue entry shows up so the response is a
    # usable recovery hint instead of a guessing game.
    catalogue = sorted_overridable_patterns()
    for pattern in catalogue:
        assert pattern in body
    # And the post-auth ``auth.resend_verification`` survives the
    # split as a sanity check on membership.
    assert "auth.resend_verification" in body


@pytest.mark.asyncio
async def test_update_rejects_unknown_endpoint_pattern(session_factory):
    """Same catalogue guard applies on PATCH so an operator can't
    typo a rename either. Uses ``reports.query`` as the seed pattern
    since it is in the OVERRIDABLE set (post auth.login moved to
    PRE_AUTH).
    """
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    override_id = resp.json()["id"]
    resp = client.patch(
        f"/api/v1/admin/rate-limit-overrides/{override_id}",
        json={"endpoint_pattern": "transactiosn.list"},
    )
    assert resp.status_code == 422, resp.text
    assert "transactiosn.list" in resp.text
    assert "endpoint_pattern_unknown" in resp.text


@pytest.mark.asyncio
async def test_create_rejects_pre_auth_endpoint_pattern(session_factory):
    """A pre-auth pattern is rejected at the schema layer with a
    distinct typed error code. Otherwise the row would persist and
    silently no-op at request time, which is exactly the operator-
    confusing footgun the architect-locked split closes.
    """
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "auth.login",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.text
    # Typed code distinct from the unknown-pattern code so the admin
    # UI can render a different message.
    assert "endpoint_pattern_pre_auth_non_overridable" in body
    # The offending pattern is echoed back.
    assert "auth.login" in body
    # And the operator is pointed at the actual fix (slowapi decorator
    # in code, not an override row).
    assert "slowapi" in body


@pytest.mark.asyncio
async def test_update_rejects_pre_auth_endpoint_pattern(session_factory):
    """PATCH cannot rename an override to a pre-auth pattern either.
    Same typed code as create so the UI handler is one branch.
    """
    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/rate-limit-overrides",
        json={
            "org_id": seeded["org_id"],
            "endpoint_pattern": "reports.query",
            "max_requests": 100,
            "period_seconds": 60,
        },
    )
    override_id = resp.json()["id"]
    resp = client.patch(
        f"/api/v1/admin/rate-limit-overrides/{override_id}",
        json={"endpoint_pattern": "auth.register"},
    )
    assert resp.status_code == 422, resp.text
    assert "endpoint_pattern_pre_auth_non_overridable" in resp.text
    assert "auth.register" in resp.text


@pytest.mark.asyncio
async def test_endpoint_catalogue_returns_split_shape(session_factory):
    """The GET catalogue endpoint returns the two-array shape the
    admin UI consumes:

    * ``overridable`` — patterns the schema validator accepts. Sorted
      deterministically so the dropdown order is stable across
      requests.
    * ``pre_auth_informational`` — patterns surfaced for context only.
      The UI renders them as disabled options. Sorted so the
      informational list is stable too.
    """
    from app.rate_limit_endpoint_catalogue import (
        OVERRIDABLE_ENDPOINT_PATTERNS,
        PRE_AUTH_ENDPOINT_PATTERNS,
        sorted_overridable_patterns,
        sorted_pre_auth_patterns,
    )

    seeded = await _seed(session_factory)
    app = _make_app(session_factory, _resolver_for(seeded, "superadmin"))
    client = TestClient(app)
    resp = client.get(
        "/api/v1/admin/rate-limit-overrides/endpoint-catalogue"
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert set(payload.keys()) == {"overridable", "pre_auth_informational"}
    assert payload["overridable"] == sorted_overridable_patterns()
    assert payload["pre_auth_informational"] == sorted_pre_auth_patterns()
    # The two sets must be disjoint — a pattern is either overridable
    # or pre-auth, never both.
    assert (
        OVERRIDABLE_ENDPOINT_PATTERNS & PRE_AUTH_ENDPOINT_PATTERNS == set()
    )
    # Sanity-check the resend_verification split: post-auth is
    # overridable, public is pre-auth.
    assert "auth.resend_verification" in payload["overridable"]
    assert "auth.resend_verification_public" in payload["pre_auth_informational"]


@pytest.mark.asyncio
async def test_endpoint_catalogue_non_superadmin_403(session_factory):
    """The catalogue endpoint is superadmin-gated like the rest of
    the router. A regular member 403s, not 200-with-list (which
    would leak the route inventory to non-admins).
    """
    seeded = await _seed(session_factory)
    for who in ("org_admin", "member"):
        app = _make_app(session_factory, _resolver_for(seeded, who))
        client = TestClient(app)
        resp = client.get(
            "/api/v1/admin/rate-limit-overrides/endpoint-catalogue"
        )
        assert resp.status_code == 403, who
