"""Router coverage for ``GET /api/v1/admin/users`` and
``GET /api/v1/admin/users/{user_id}`` (L4.4 slice).

Service-level behavior is pinned in
``tests/services/test_admin_users_search_service.py``. This file
covers:

- Auth gate: missing ``users.view`` returns 403.
- 404 on unknown user id.
- Audit-throttle behavior: first list-view writes a row, second
  within the window does not. Same per-(actor, target) for detail.
- Privacy: raw ``q`` string never appears in structlog event detail.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.routers.admin_users import (
    _reset_audit_throttle_for_tests,
    router as admin_users_router,
)
from app.security import hash_password


@pytest_asyncio.fixture(autouse=True)
def _reset_throttle():
    """Clear the in-process audit throttle before EVERY test so the
    throttle in one test never bleeds into the next."""
    _reset_audit_throttle_for_tests()
    yield
    _reset_audit_throttle_for_tests()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_app(session_factory, actor_user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        async with session_factory() as db:
            user = await db.get(User, actor_user_id)
            assert user is not None
            return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(admin_users_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target_org = Organization(name="Target", billing_cycle_day=1)
        db.add_all([admin_org, target_org])
        await db.commit()

        sa = User(
            org_id=admin_org.id, username="root", email="root@platform.io",
            password_hash=hash_password("pw"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=admin_org.id, username="member", email="member@platform.io",
            password_hash=hash_password("pw"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        target = User(
            org_id=target_org.id, username="target_owner",
            email="t_owner@target.io",
            password_hash=hash_password("pw"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, member, target])
        await db.commit()
        return {
            "sa_id": sa.id,
            "member_id": member.id,
            "target_id": target.id,
            "admin_org_id": admin_org.id,
            "target_org_id": target_org.id,
        }


# ── auth gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_requires_users_view(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["member_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_user_detail_requires_users_view(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["member_id"])
    client = TestClient(app)
    resp = client.get(f"/api/v1/admin/users/{ids['target_id']}")
    assert resp.status_code == 403


# ── happy paths ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_superadmin_sees_cross_org(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    emails = {item["email"] for item in body["items"]}
    assert "t_owner@target.io" in emails  # cross-org visible.


@pytest.mark.asyncio
async def test_list_users_q_filters(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users", params={"q": "target"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "t_owner@target.io"


@pytest.mark.asyncio
async def test_get_user_detail_returns_payload(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get(f"/api/v1/admin/users/{ids['target_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == ids["target_id"]
    assert body["email"] == "t_owner@target.io"
    assert body["orgs"][0]["org_id"] == ids["target_org_id"]
    assert isinstance(body["recent_audit_events"], list)


@pytest.mark.asyncio
async def test_get_user_detail_404(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users/9999999")
    assert resp.status_code == 404


# ── sort contract (PR: shared list contract) ─────────────────────────


@pytest.mark.asyncio
async def test_list_users_sort_email_asc(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get(
        "/api/v1/admin/users", params={"sort_by": "email", "sort_dir": "asc"}
    )
    assert resp.status_code == 200
    emails = [item["email"] for item in resp.json()["items"]]
    assert emails == sorted(emails)


@pytest.mark.asyncio
async def test_list_users_sort_email_desc_reverses(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get(
        "/api/v1/admin/users", params={"sort_by": "email", "sort_dir": "desc"}
    )
    assert resp.status_code == 200
    emails = [item["email"] for item in resp.json()["items"]]
    assert emails == sorted(emails, reverse=True)


@pytest.mark.asyncio
async def test_list_users_default_sort_is_created_at_desc(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 200
    items = resp.json()["items"]
    created = [item["created_at"] for item in items]
    # Default ordering is created_at desc; with equal timestamps the id
    # desc tiebreaker keeps it deterministic. Either way it is sorted
    # descending (non-increasing) on created_at.
    assert created == sorted(created, reverse=True)


@pytest.mark.asyncio
async def test_list_users_invalid_sort_by_returns_400(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users", params={"sort_by": "not_a_column"})
    assert resp.status_code == 400
    assert "invalid_sort_by" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_users_total_unaffected_by_limit_offset(session_factory) -> None:
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users", params={"limit": 1, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    # Three seeded users: total reflects the full filtered set, the page
    # is bounded by limit.
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["limit"] == 1
    assert body["offset"] == 1


# ── envelope conformance (PR: shared list contract) ──────────────────


@pytest.mark.asyncio
async def test_list_users_response_conforms_to_list_envelope(session_factory) -> None:
    """The list response must validate against ``ListEnvelope`` and expose
    exactly the envelope keys.

    The service returns a plain dict (not a per-row Pydantic model), so this
    is the guardrail that catches shape drift before the other four admin
    tables copy the pattern.
    """
    from app.schemas.common import ListEnvelope

    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 200
    body = resp.json()

    # Exactly the envelope keys — no more, no less.
    assert set(body.keys()) == {"items", "total", "limit", "offset"}

    # Validates against the generic envelope (rows kept as opaque dicts).
    ListEnvelope[dict].model_validate(body)


# ── audit throttle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_audit_throttle_writes_once_per_window(session_factory) -> None:
    """Three GETs in the same window: exactly one audit row."""
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    for _ in range(3):
        resp = client.get("/api/v1/admin/users")
        assert resp.status_code == 200
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.user.list.viewed"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    # Audit detail carries the bounded fields (no raw q allowed).
    detail = rows[0].detail
    assert detail is not None
    assert "query_length" in detail
    assert "result_count" in detail
    assert "q" not in detail  # raw search string must not be persisted


@pytest.mark.asyncio
async def test_detail_audit_throttle_per_target(session_factory) -> None:
    """Two different targets within the same window: two audit rows."""
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)
    # Same actor opens two different users; each should record.
    client.get(f"/api/v1/admin/users/{ids['target_id']}")
    client.get(f"/api/v1/admin/users/{ids['target_id']}")
    client.get(f"/api/v1/admin/users/{ids['member_id']}")
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.user.viewed"
                )
            )
        ).scalars().all()
    target_ids = sorted([r.detail["target_user_id"] for r in rows])
    assert target_ids == sorted([ids["target_id"], ids["member_id"]])


# ── privacy: raw q never in audit detail ──────────────────────────────


@pytest.mark.asyncio
async def test_raw_q_not_in_audit_detail(session_factory) -> None:
    """A successful list query never persists ``q`` itself.

    The audit row carries ``query_length`` (an int) plus other metadata,
    but the raw ``q`` string must not appear anywhere in
    ``detail``. This mirrors the description-suggestions privacy
    contract (Wave 2A section 5.4).
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=ids["sa_id"])
    client = TestClient(app)

    secret = "supersecret-query-XYZ"
    resp = client.get("/api/v1/admin/users", params={"q": secret})
    assert resp.status_code == 200

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.user.list.viewed"
                )
            )
        ).scalars().all()

    # We expect exactly one row (first-call always records).
    assert len(rows) == 1
    detail = rows[0].detail or {}
    # ``q`` itself must be absent. ``query_length`` is the only echo
    # of search input the audit row carries.
    assert "q" not in detail
    assert "query" not in detail
    assert detail.get("query_length") == len(secret)
    # Defensive: serialize the entire row and assert the secret string
    # is not embedded anywhere (e.g., a future field rename).
    import json
    assert secret not in json.dumps(detail)
