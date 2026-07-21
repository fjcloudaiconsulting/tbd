"""Router + service tests for Track D — PATCH /api/v1/orgs/{org_id}/rename.

The endpoint is owner-scoped to the caller's own organization. These
tests pin:

- Auth gate (OWNER only; ADMIN/MEMBER → 403; unauthenticated → 401/403).
- Cross-tenant blocking (path ``org_id`` must equal the caller's
  own ``org_id``).
- Validation rules (empty after trim, > 80 chars).
- Whitespace normalization (Pydantic-side trim + collapse).
- Case-insensitive uniqueness via the migration's UNIQUE on
  ``LOWER(name)``.
- Accent sensitivity (Café vs Cafe stays distinct).
- Same-name no-op (no audit row, no DB write).
- Audit-event emission (success on the request session,
  failure on an independent session — survives rollback).

Service-layer behavior is also covered here for the ``rename_org``
helper since the surface is small enough to keep in one file.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.notification import Notification, NotificationCategory
from app.models.user import Organization, Role, User
from app.routers.orgs import router as orgs_router
from app.security import hash_password
from app.services import org_service


# ── Fixture: in-memory aiosqlite + FK enforcement + UNIQUE LOWER(name) ──────


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
        # Mirror the migration's UNIQUE INDEX on LOWER(name) so the
        # case-insensitive constraint is exercised in tests too.
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_organizations_name_normalized "
            "ON organizations (LOWER(name))"
        ))

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ── Test app builder ───────────────────────────────────────────────────────


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user(request: Request) -> User:
        request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
        return await current_user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(orgs_router)
    return app


# ── Seed helpers ───────────────────────────────────────────────────────────


ORG_A_NAME = "Acme Household"
ORG_B_NAME = "Wayne Household"


async def _seed(factory, *, second_org_name: str | None = None) -> dict:
    """One org with owner + admin + member, plus an optional second
    org for cross-tenant / dup-name tests.
    """
    async with factory() as db:
        org_a = Organization(name=ORG_A_NAME, billing_cycle_day=1)
        db.add(org_a)
        await db.commit()

        owner = User(
            org_id=org_a.id, username="owner", email="o@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        admin = User(
            org_id=org_a.id, username="admin", email="a@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org_a.id, username="member", email="m@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([owner, admin, member])
        await db.commit()

        org_b_id = None
        if second_org_name is not None:
            org_b = Organization(name=second_org_name, billing_cycle_day=1)
            db.add(org_b)
            await db.commit()
            org_b_id = org_b.id

        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b_id,
            "owner_id": owner.id,
            "admin_id": admin.id,
            "member_id": member.id,
        }


def _resolver_for(role: Role):
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.role == role))
            ).scalar_one()
    return resolve


async def _audit_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).order_by(AuditEvent.id.asc())
            )
        ).scalars().all()
        return list(rows)


# ── Auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_success_owner(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "Acme Inc"},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == seed["org_a_id"]
    assert body["name"] == "Acme Inc"
    assert body["billing_cycle_day"] == 1

    # DB updated.
    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seed["org_a_id"])
        )).scalar_one()
        assert org.name == "Acme Inc"

    # Success audit row written via the request session.
    rows = await _audit_rows(session_factory)
    success = [r for r in rows if r.event_type == "org.rename" and r.outcome == AuditOutcome.SUCCESS]
    assert len(success) == 1
    row = success[0]
    assert row.actor_user_id == seed["owner_id"]
    assert row.actor_email == "o@acme.io"
    assert row.target_org_id == seed["org_a_id"]
    assert row.target_org_name == "Acme Inc"
    assert row.detail == {"old_name": ORG_A_NAME, "new_name": "Acme Inc"}


@pytest.mark.asyncio
async def test_rename_org_admin_forbidden(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "Acme Inc"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_rename_org_member_forbidden(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.MEMBER))
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "Acme Inc"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_rename_org_unauthenticated(session_factory):
    """No `get_current_user` override → real HTTPBearer dep runs and
    rejects the missing Authorization header. FastAPI's HTTPBearer
    returns 403 on missing creds (not 401), same behaviour every
    other authed route exhibits in this app.
    """
    seed = await _seed(session_factory)
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(orgs_router)

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "Acme Inc"},
        )
    assert res.status_code in (401, 403)


# ── Uniqueness ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_duplicate_case_insensitive(session_factory):
    """OWNER tries to rename their org to a name another org already
    holds (case-insensitive). Returns 409 and writes a failure-path
    audit row via the independent session.
    """
    seed = await _seed(session_factory, second_org_name="Acme")
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "acme"},
        )

    assert res.status_code == 409
    assert res.json() == {"detail": "An organization with that name already exists"}

    # No success row, one failure row with attempted name.
    rows = await _audit_rows(session_factory)
    success = [r for r in rows if r.outcome == AuditOutcome.SUCCESS]
    failure = [r for r in rows if r.outcome == AuditOutcome.FAILURE]
    assert success == []
    assert len(failure) == 1
    fail = failure[0]
    assert fail.event_type == "org.rename"
    assert fail.actor_user_id == seed["owner_id"]
    assert fail.target_org_id == seed["org_a_id"]
    assert fail.target_org_name == "acme"
    assert fail.detail == {"attempted_name": "acme"}

    # Original org name unchanged.
    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seed["org_a_id"])
        )).scalar_one()
        assert org.name == ORG_A_NAME


@pytest.mark.asyncio
async def test_rename_org_duplicate_accent_sensitive(session_factory):
    """SQLite's LOWER() folds ASCII case but is binary on accents,
    matching the MySQL migration's ``utf8mb4_0900_as_cs`` collation.
    'Cafe' and 'Café' coexist.
    """
    seed = await _seed(session_factory, second_org_name="Cafe")
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "Café"},
        )

    assert res.status_code == 200, res.text

    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seed["org_a_id"])
        )).scalar_one()
        assert org.name == "Café"


# ── No-op ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_no_op_same_name(session_factory):
    """Submitting the current name short-circuits to a 200 with the
    unchanged row. NO audit row, NO DB write.
    """
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": ORG_A_NAME},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["name"] == ORG_A_NAME

    rows = await _audit_rows(session_factory)
    assert rows == []  # neither success nor failure rows


@pytest.mark.asyncio
async def test_rename_org_no_op_only_whitespace_difference(session_factory):
    """``"  Acme   Household  "`` collapses to ``"Acme Household"``
    via Pydantic's normalizer, which equals the current name → no-op.
    """
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "  Acme   Household  "},
        )

    assert res.status_code == 200
    rows = await _audit_rows(session_factory)
    assert rows == []


# ── Validation ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_validation_empty_after_trim(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "   "},
        )

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_rename_org_validation_too_long(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))
    too_long = "A" * 81

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": too_long},
        )

    assert res.status_code == 422


# ── Cross-tenant ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_cross_tenant_blocked(session_factory):
    """OWNER of org A targets ``/api/v1/orgs/{org_b_id}/rename`` →
    403. The owner-only role gate is satisfied, but the path id
    doesn't match ``current_user.org_id``.
    """
    seed = await _seed(session_factory, second_org_name="Wayne Household")
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_b_id']}/rename",
            json={"name": "Wayne Inc"},
        )

    assert res.status_code == 403


# ── Service unit test ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_normalizes_whitespace_in_preflight(session_factory):
    """Direct ``rename_org`` call with a whitespace-collapsed name
    that resolves to the current name (case-insensitively) returns
    a no-op tuple ``(old, old)`` without raising. This pins the
    service-level guard independent of the Pydantic layer.
    """
    seed = await _seed(session_factory)
    async with session_factory() as db:
        old, new = await org_service.rename_org(
            db, org_id=seed["org_a_id"], new_name="acme household",
        )
        assert old == ORG_A_NAME
        assert new == ORG_A_NAME


@pytest.mark.asyncio
async def test_rename_org_service_preflight_is_accent_sensitive(session_factory):
    """Service-level pin for the accent-sensitive preflight.

    Regression: the original preflight used
    ``Organization.name.ilike(new_name)``. On MySQL with the base
    column's default ``utf8mb4_0900_ai_ci`` collation, ``ilike``
    treats "Cafe" and "Café" as equal and would 409 a rename that
    the actual UNIQUE on the ``utf8mb4_0900_as_cs``-collated
    ``name_normalized`` column would happily accept.

    There's a route-level test (``test_rename_org_duplicate_accent_sensitive``)
    that exercises this path through the API on SQLite, but the SQL
    operator behaviour differs by dialect. This test calls the
    service directly so the intent — "accent-different names are NOT
    duplicates" — is greppable at the comparison's site.
    """
    seed = await _seed(session_factory, second_org_name="Cafe")
    async with session_factory() as db:
        old, new = await org_service.rename_org(
            db, org_id=seed["org_a_id"], new_name="Café",
        )
        assert old == ORG_A_NAME
        assert new == "Café"


@pytest.mark.asyncio
async def test_rename_org_service_preflight_does_not_treat_underscore_as_wildcard(
    session_factory,
):
    """Service-level pin that the preflight uses exact comparison,
    not SQL ``LIKE`` wildcards.

    Regression: ``ilike("FooXbar")`` would NOT have matched a row
    named "Foo_bar" — wrong direction for that example. The real
    risk with ``ilike`` is the inverse: ``ilike("Foo_bar")`` matches
    "FooXbar" because ``_`` is the single-char wildcard. Seed an org
    "Foo_bar", then attempt to rename to "Foo_bar" exactly while
    another org "FooXbar" exists. The exact match should still
    409 (collision with self-named row). The flip case — exists
    "FooXbar", rename target "Foo_bar" — should NOT 409 because
    the names are genuinely different.
    """
    seed = await _seed(session_factory, second_org_name="FooXbar")
    async with session_factory() as db:
        # Distinct names, distinct rows: this rename must succeed.
        old, new = await org_service.rename_org(
            db, org_id=seed["org_a_id"], new_name="Foo_bar",
        )
        assert old == ORG_A_NAME
        assert new == "Foo_bar"


@pytest.mark.asyncio
async def test_rename_org_service_preflight_case_insensitive_still_blocks(
    session_factory,
):
    """Companion to the accent-sensitive test: confirm the preflight
    still raises 409 when the new name differs from another org's
    name only by ASCII case. Belt-and-suspenders alongside the
    route-level case-insensitive test.
    """
    from fastapi import HTTPException

    seed = await _seed(session_factory, second_org_name="Acme")
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await org_service.rename_org(
                db, org_id=seed["org_a_id"], new_name="ACME",
            )
        assert exc.value.status_code == 409


# ── PR4: org.renamed fanout to org admins ──────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_fans_out_org_admin_notification(session_factory):
    """A successful rename fans out an ``org.renamed`` in-app
    notification to every active org admin (OWNER + ADMIN), NOT the
    plain MEMBER, carrying old + new name and the audit id."""
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/orgs/{seed['org_a_id']}/rename",
            json={"name": "Acme Inc"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        notifs = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "org.renamed"
                )
            )
        ).scalars().all()
        audit_rows = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.event_type == "org.rename")
            )
        ).scalars().all()

    recipients = {n.user_id for n in notifs}
    # OWNER + ADMIN get it; the plain MEMBER does not.
    assert recipients == {seed["owner_id"], seed["admin_id"]}
    assert all(n.category == NotificationCategory.ORG_ADMIN for n in notifs)
    assert all(ORG_A_NAME in n.body and "Acme Inc" in n.body for n in notifs)
    assert len(audit_rows) == 1
    assert all(n.audit_event_id == audit_rows[0].id for n in notifs)
