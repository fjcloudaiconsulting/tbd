"""Tag router tests (PR-Tags-A).

End-to-end coverage of the HTTP surface plus audit-event wiring. The
in-memory SQLite + dependency-override pattern matches
``test_admin_audit.py`` / ``test_audit_wiring.py``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.audit_event import AuditEvent
from app.models.category import Category, CategoryType
from app.models.settings import OrgSetting
from app.models.tag import (
    Tag,
    TagDictionary,
    TagDictionaryContributor,
    TransactionTag,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.tags import router as tags_router, transaction_tags_router
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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_basic(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Tags Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root",
            email="root@x.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_active=True, email_verified=True,
        )
        db.add(user)
        await db.flush()
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=False
        )
        db.add(at)
        await db.flush()
        acc = Account(
            org_id=org.id, name="Main", account_type_id=at.id,
            currency="EUR", balance=Decimal("1000.00"), is_default=True,
        )
        db.add(acc)
        cat = Category(
            org_id=org.id, name="Insurance", slug="insurance",
            is_system=False, type=CategoryType.EXPENSE,
        )
        db.add(cat)
        await db.flush()
        tx = Transaction(
            org_id=org.id, account_id=acc.id, category_id=cat.id,
            description="Premium", amount=Decimal("12.34"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 5, 1),
            settled_date=datetime.date(2026, 5, 1),
        )
        db.add(tx)
        await db.commit()
        return {
            "org_id": org.id, "user_id": user.id,
            "category_id": cat.id, "transaction_id": tx.id,
        }


def make_app(factory, user_id: int):
    from fastapi.responses import JSONResponse
    from app.services.exceptions import ConflictError, NotFoundError, ValidationError

    app = FastAPI()

    @app.exception_handler(NotFoundError)
    async def _nf(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_current_user() -> User:
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    def override_session_factory():
        return factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(tags_router)
    app.include_router(transaction_tags_router)
    return app


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tag_endpoint_success(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/tags", json={"name": "Insurance"})
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Insurance"
    assert body["name_normalized"] == "insurance"


@pytest.mark.asyncio
async def test_create_tag_audit_event_emitted(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post("/api/v1/tags", json={"name": "Insurance"})
    assert res.status_code == 201
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    types = [r.event_type for r in rows]
    assert "tag.created" in types
    audit = next(r for r in rows if r.event_type == "tag.created")
    assert audit.detail["name"] == "insurance"
    assert audit.outcome.value == "success"


@pytest.mark.asyncio
async def test_create_tag_collision_returns_409(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        client.post("/api/v1/tags", json={"name": "Insurance"})
        res = client.post("/api/v1/tags", json={"name": "INSURANCE"})
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_list_tags_returns_usage_count(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        client.post("/api/v1/tags", json={"name": "insurance"})
        client.put(
            f"/api/v1/transactions/{seeds['transaction_id']}/tags",
            json={"tag_names": ["insurance"]},
        )
        res = client.get("/api/v1/tags")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["name"] == "insurance"
    assert body[0]["usage_count"] == 1


@pytest.mark.asyncio
async def test_rename_tag_emits_audit_event(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        created = client.post("/api/v1/tags", json={"name": "ins"}).json()
        res = client.patch(
            f"/api/v1/tags/{created['id']}",
            json={"name": "insurance"},
        )
    assert res.status_code == 200
    assert res.json()["name_normalized"] == "insurance"
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    types = [r.event_type for r in rows]
    assert "tag.renamed" in types


@pytest.mark.asyncio
async def test_delete_tag_emits_audit_event(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        created = client.post("/api/v1/tags", json={"name": "insurance"}).json()
        res = client.delete(f"/api/v1/tags/{created['id']}")
    assert res.status_code == 204
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    types = [r.event_type for r in rows]
    assert "tag.deleted" in types


@pytest.mark.asyncio
async def test_replace_transaction_tags_endpoint(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/transactions/{seeds['transaction_id']}/tags",
            json={"tag_names": ["insurance", "monthly"]},
        )
    assert res.status_code == 200
    body = res.json()
    assert {row["name_normalized"] for row in body} == {"insurance", "monthly"}
    async with session_factory() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    types = [r.event_type for r in rows]
    assert "transaction.tags.replaced" in types


@pytest.mark.asyncio
async def test_replace_transaction_tags_cap_returns_422(session_factory):
    seeds = await _seed_basic(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/transactions/{seeds['transaction_id']}/tags",
            json={"tag_names": ["a", "b", "c", "d", "e", "f"]},
        )
    # Pydantic Field max_length triggers a 422 before service runs.
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_suggest_endpoint_three_pass_precedence(session_factory):
    seeds = await _seed_basic(session_factory)
    # Enable sharing + seed a dictionary entry above the floor.
    async with session_factory() as db:
        db.add(OrgSetting(
            org_id=seeds["org_id"], key="share_tag_data", value="true",
        ))
        db.add(TagDictionary(
            name_normalized="insurance",
            contributor_org_count=5,
            usage_count=42,
            is_seed=False,
        ))
        await db.commit()

    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        # Tag the seeded transaction with "insurance" so the
        # org_co_category pass has something to return.
        client.put(
            f"/api/v1/transactions/{seeds['transaction_id']}/tags",
            json={"tag_names": ["insurance"]},
        )
        res = client.get(
            f"/api/v1/tags/suggest?prefix=ins&category_id={seeds['category_id']}"
        )
    assert res.status_code == 200
    body = res.json()
    suggestions = body["suggestions"]
    # First hit: org_co_category for "insurance".
    assert suggestions[0]["name"] == "insurance"
    assert suggestions[0]["source"] == "org_co_category"
