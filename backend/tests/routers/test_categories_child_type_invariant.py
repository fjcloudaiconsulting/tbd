"""Router-level test for the child-type invariant on category create/update.

Closes the final Medium finding from PR #150 review: a child category could
diverge from its master via:
- POST /api/v1/categories/ with parent_id + a body.type different from the
  parent's type, which the route would persist verbatim.
- PUT /api/v1/categories/{id} on a child with body.type different from the
  parent's type when no incompatible references existed yet (the existing
  reference-based guard wouldn't fire on a fresh child).

Rules enforced:
- Create with parent_id: child.type is forced to the parent's type. Body's
  type is silently ignored. parent_id is the authoritative signal.
- Update on a child (cat.parent_id is not None) with body.type set:
  reject with 400 when body.type differs from parent.type. Skip silently
  when it matches. Other fields update normally when type is omitted.
- Master cascade (parent_id is None) keeps its existing behavior — type
  change cascades to children's types in the same operation.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.user import Role, User
from app.routers.categories import router as categories_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory():
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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


def make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(
                    select(User).where(User.is_superadmin.is_(True))
                )
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(categories_router)
    return app


async def _seed(factory) -> dict:
    """One org, an EXPENSE master + an INCOME master + a BOTH master.

    The EXPENSE master has one existing child (typed EXPENSE) so we can test
    update behavior on a pre-existing subcategory.
    """
    async with factory() as db:
        org = Organization(name="T", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="r@x.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True,
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Main", account_type_id=at.id,
            balance=Decimal("0"), currency="EUR",
        )
        expense_master = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        income_master = Category(
            org_id=org.id, name="Salary", slug="salary",
            type=CategoryType.INCOME,
        )
        both_master = Category(
            org_id=org.id, name="Flex", slug="flex", type=CategoryType.BOTH,
        )
        db.add_all([acct, expense_master, income_master, both_master])
        await db.flush()
        # A pre-existing expense child under the expense master (mirrors
        # what org_bootstrap seeds: child.type == master.type).
        expense_child = Category(
            org_id=org.id, name="Supermarket", slug="supermarket",
            type=CategoryType.EXPENSE, parent_id=expense_master.id,
        )
        db.add(expense_child)
        await db.commit()
        return {
            "org_id": org.id,
            "expense_master_id": expense_master.id,
            "income_master_id": income_master.id,
            "both_master_id": both_master.id,
            "expense_child_id": expense_child.id,
        }


# ── Create path: child silently inherits parent's type ───────────────────────

@pytest.mark.asyncio
async def test_create_child_inherits_parent_type_overriding_body(session_factory):
    """POST with parent_id=<expense> and body.type=income persists EXPENSE."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories",
            json={
                "name": "New Sub",
                "type": "income",
                "parent_id": seed["expense_master_id"],
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["type"] == "expense"
    assert resp.json()["parent_id"] == seed["expense_master_id"]


@pytest.mark.asyncio
async def test_create_child_without_body_type_inherits_parent(session_factory):
    """POST with parent_id and no `type` field persists parent's type."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        # CategoryCreate.type defaults to "both"; we still expect inheritance
        # because the parent_id is the authoritative signal.
        resp = client.post(
            "/api/v1/categories",
            json={"name": "Another Sub", "parent_id": seed["income_master_id"]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["type"] == "income"


@pytest.mark.asyncio
async def test_create_master_uses_body_type(session_factory):
    """POST with parent_id=None preserves body.type unchanged."""
    seed = await _seed(session_factory)  # noqa: F841
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories",
            json={"name": "New Master", "type": "income"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["type"] == "income"
    assert resp.json()["parent_id"] is None


@pytest.mark.asyncio
async def test_create_child_under_other_org_parent_returns_400(session_factory):
    """Existing org-isolation behavior must still fire (parent in another org)."""
    seed = await _seed(session_factory)  # noqa: F841
    # Seed a second org with its own master. Since the dependency override
    # always returns the first superadmin, the second master_id is
    # cross-org from the API's perspective.
    async with session_factory() as db:
        other_org = Organization(name="Other", billing_cycle_day=1)
        db.add(other_org)
        await db.flush()
        other_master = Category(
            org_id=other_org.id, name="Foreign", slug="foreign",
            type=CategoryType.EXPENSE,
        )
        db.add(other_master)
        await db.commit()
        other_master_id = other_master.id
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories",
            json={
                "name": "Sub",
                "type": "expense",
                "parent_id": other_master_id,
            },
        )
    assert resp.status_code == 400


# ── Update path: child rejects mismatched type ───────────────────────────────

@pytest.mark.asyncio
async def test_update_child_with_mismatched_type_rejected(session_factory):
    """PUT child with body.type different from parent's type → 400, atomic."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_child_id']}",
            json={"name": "Renamed Child", "type": "income"},
        )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"].lower()
    assert "subcategory" in detail or "child" in detail
    assert "parent" in detail or "master" in detail
    # The name change must NOT have been persisted (atomic rejection).
    async with session_factory() as db:
        child = (await db.execute(
            select(Category).where(Category.id == seed["expense_child_id"])
        )).scalar_one()
        assert child.name == "Supermarket"
        assert child.type == CategoryType.EXPENSE


@pytest.mark.asyncio
async def test_update_child_with_matching_type_succeeds(session_factory):
    """PUT child with body.type equal to parent's type → 200, no-op on type."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_child_id']}",
            json={"type": "expense"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "expense"


@pytest.mark.asyncio
async def test_update_child_other_fields_when_type_omitted(session_factory):
    """PUT child with only name change → 200, type unchanged."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_child_id']}",
            json={"name": "Big Grocer"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Big Grocer"
    assert resp.json()["type"] == "expense"


# ── Master cascade still works ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_master_widen_to_both_cascades_to_children(session_factory):
    """Master EXPENSE → BOTH (always-safe widen): child flips to BOTH too."""
    seed = await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_master_id']}",
            json={"type": "both"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "both"
    # Child was cascaded.
    async with session_factory() as db:
        child = (await db.execute(
            select(Category).where(Category.id == seed["expense_child_id"])
        )).scalar_one()
        assert child.type == CategoryType.BOTH
