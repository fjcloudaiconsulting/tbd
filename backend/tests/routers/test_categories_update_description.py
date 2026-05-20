"""Router-level tests for PUT /api/v1/categories/{id} description handling.

Regression: PR #324 surfaced that the route used
``if body.description is not None: cat.description = body.description``,
which silently dropped an explicit ``null`` payload. Clearing an existing
description never persisted. The fix uses Pydantic v2
``model_fields_set`` to distinguish "field omitted from the body" from
"field explicitly set to null".

These tests pin both halves of that distinction:
    * PUT {"description": null} on a row whose description is non-null
      MUST clear the DB column to NULL.
    * PUT without ``description`` in the body MUST leave the existing
      description unchanged.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

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
from app.models import Category, Organization
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


async def _seed_with_description(factory, description: str | None) -> dict:
    """Seed an org with the C0 floor satisfied plus one row to mutate."""
    async with factory() as db:
        org = Organization(name="T", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="r@x.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        # Floor: >= 1 income master + 1 expense master + 1 sub per type.
        income_master = Category(
            org_id=org.id, name="Income", slug="income_m",
            type=CategoryType.INCOME,
        )
        expense_master = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
        )
        # The row under test — a top-level expense master with the seeded
        # description. We mutate this one's description across the tests.
        target = Category(
            org_id=org.id, name="Lifestyle", slug="lifestyle",
            type=CategoryType.EXPENSE, description=description,
        )
        db.add_all([user, income_master, expense_master, target])
        await db.flush()
        # Subs to satisfy the floor for both types.
        db.add_all([
            Category(
                org_id=org.id, name="Salary", parent_id=income_master.id,
                type=CategoryType.INCOME,
            ),
            Category(
                org_id=org.id, name="Food", parent_id=expense_master.id,
                type=CategoryType.EXPENSE,
            ),
        ])
        await db.commit()
        return {"org_id": org.id, "target_id": target.id}


async def _fetch_description(factory, category_id: int) -> str | None:
    async with factory() as db:
        return await db.scalar(
            select(Category.description).where(Category.id == category_id)
        )


@pytest.mark.asyncio
async def test_put_explicit_null_clears_existing_description(session_factory):
    """PUT {"description": null} MUST persist NULL to the DB column."""
    seed = await _seed_with_description(session_factory, "old value")
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['target_id']}",
            json={"name": "Lifestyle", "description": None},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] is None
    stored = await _fetch_description(session_factory, seed["target_id"])
    assert stored is None, (
        "explicit-null payload must clear description to NULL, "
        f"got {stored!r}"
    )


@pytest.mark.asyncio
async def test_put_omitting_description_preserves_existing_value(session_factory):
    """PUT without the ``description`` key MUST leave the column untouched."""
    seed = await _seed_with_description(session_factory, "keep me")
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['target_id']}",
            json={"name": "Renamed Only"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "keep me"
    stored = await _fetch_description(session_factory, seed["target_id"])
    assert stored == "keep me", (
        "omitted-description payload must leave existing description "
        f"intact, got {stored!r}"
    )


@pytest.mark.asyncio
async def test_put_new_description_value_persists(session_factory):
    """Sanity: setting a non-null description still works."""
    seed = await _seed_with_description(session_factory, None)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['target_id']}",
            json={"description": "fresh value"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "fresh value"
    stored = await _fetch_description(session_factory, seed["target_id"])
    assert stored == "fresh value"
