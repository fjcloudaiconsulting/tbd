"""BE-18 over HTTP: no cross-org count in the API, contributions withdrawn
when sharing is turned off through the settings routes, and a rate limit on
both routes that create tags. Service-level counting rules are in
``tests/services/test_tag_dictionary_hardening.py``.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.settings import OrgSetting
from app.models.tag import Tag, TagDictionary, TagDictionaryContributor
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.settings import router as settings_router
from app.routers.tags import router as tags_router, transaction_tags_router
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services import tag_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from tests.factories import make_test_app

AGED = utcnow_naive() - datetime.timedelta(days=30)


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


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


async def _seed(factory) -> dict:
    """The acting org (owner ``root``) plus three aged orgs that all share
    ``insurance``; every org shares tag data."""
    async with factory() as db:
        orgs = []
        for name in ("Mine", "A", "B", "C"):
            org = Organization(name=name, billing_cycle_day=1, created_at=AGED)
            db.add(org)
            await db.flush()
            db.add(OrgSetting(org_id=org.id, key="share_tag_data", value="true"))
            orgs.append(org.id)
        user = User(org_id=orgs[0], username="root", email="root@x.io",
                    password_hash=hash_password("pw-1234567"), role=Role.OWNER,
                    is_active=True, email_verified=True)
        db.add(user)
        at = AccountType(org_id=orgs[0], name="Checking", slug="checking", is_system=False)
        db.add(at)
        await db.flush()
        acc = Account(org_id=orgs[0], name="Main", account_type_id=at.id, currency="EUR",
                      balance=Decimal("0"), is_default=True)
        cat = Category(org_id=orgs[0], name="Bills", slug="bills", is_system=False,
                       type=CategoryType.EXPENSE)
        db.add_all([acc, cat])
        await db.flush()
        txs = []
        for i in range(8):
            tx = Transaction(org_id=orgs[0], account_id=acc.id, category_id=cat.id,
                             description=f"T{i}", amount=Decimal("1.00"),
                             type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                             date=datetime.date(2026, 5, 1),
                             settled_date=datetime.date(2026, 5, 1))
            db.add(tx)
            txs.append(tx)
        await db.commit()
        for org_id in orgs[1:]:
            await tag_service.create_tag(db, org_id=org_id, name="insurance",
                                         created_by_user_id=None)
            await db.commit()
        return {"org_id": orgs[0], "user_id": user.id, "tx_ids": [t.id for t in txs]}


def _app(factory, user_id: int):
    async def resolve_user(session_factory) -> User:
        async with session_factory() as db:
            return (await db.execute(select(User).where(User.id == user_id))).scalar_one()

    app = make_test_app(
        factory,
        routers=[tags_router, transaction_tags_router, settings_router, transactions_router],
        current_user=resolve_user,
        override_session_factory=True,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.exception_handler(NotFoundError)
    async def _nf(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    return app


async def _my_contributions(factory, org_id: int) -> int:
    async with factory() as db:
        rows = await db.execute(select(TagDictionaryContributor.id).where(
            TagDictionaryContributor.contributor_org_id == org_id))
        return len(rows.all())


async def _counts(factory, name: str) -> tuple[int, int]:
    async with factory() as db:
        entry = await db.scalar(select(TagDictionary).where(TagDictionary.name_normalized == name))
        return entry.contributor_org_count, entry.usage_count


# ── no cross-org count ───────────────────────────────────────────────────────


async def test_suggest_returns_no_usage_count_for_shared_tags(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        res = client.get("/api/v1/tags/suggest", params={"prefix": "ins"})
    assert res.status_code == 200, res.text
    assert res.json()["suggestions"] == [
        {"name": "insurance", "source": "shared_dictionary", "weight": None}
    ]
    assert "usage_count" not in res.text


# ── opt-out through the settings routes ──────────────────────────────────────


async def _contribute_mine(client) -> None:
    for name in ("insurance", "gym"):
        assert client.post("/api/v1/tags", json={"name": name}).status_code == 201


async def test_turning_sharing_off_withdraws_contributions(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        await _contribute_mine(client)
        assert await _my_contributions(session_factory, seeds["org_id"]) == 2
        assert await _counts(session_factory, "insurance") == (4, 4)
        res = client.put("/api/v1/settings", json={"key": "share_tag_data", "value": "false"})
    assert res.status_code == 200, res.text
    assert await _my_contributions(session_factory, seeds["org_id"]) == 0
    assert await _counts(session_factory, "insurance") == (3, 3)
    assert await _counts(session_factory, "gym") == (0, 0)


async def test_deleting_the_sharing_setting_withdraws_contributions(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        await _contribute_mine(client)
        res = client.delete("/api/v1/settings/share_tag_data")
    assert res.status_code == 204, res.text
    assert await _my_contributions(session_factory, seeds["org_id"]) == 0
    assert await _counts(session_factory, "insurance") == (3, 3)


async def test_keeping_sharing_on_keeps_contributions(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        await _contribute_mine(client)
        res = client.put("/api/v1/settings", json={"key": "share_tag_data", "value": "true"})
        other = client.put("/api/v1/settings", json={"key": "something_else", "value": "false"})
    assert res.status_code == 200 and other.status_code == 200
    assert await _my_contributions(session_factory, seeds["org_id"]) == 2


async def test_deleting_a_tag_over_http_withdraws_it(session_factory):
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        tag_id = client.post("/api/v1/tags", json={"name": "insurance"}).json()["id"]
        assert await _counts(session_factory, "insurance") == (4, 4)
        assert client.delete(f"/api/v1/tags/{tag_id}").status_code == 204
    assert await _counts(session_factory, "insurance") == (3, 3)


# ── rate limits ──────────────────────────────────────────────────────────────


async def test_tag_create_is_rate_limited(session_factory):
    """Wrong implementation: no limit on the route that feeds the dictionary."""
    seeds = await _seed(session_factory)
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        for i in range(30):
            res = client.post("/api/v1/tags", json={"name": f"t{i}"})
            assert res.status_code == 201, (i, res.text)
        res = client.post("/api/v1/tags", json={"name": "one-more"})
    assert res.status_code == 429, res.text


async def test_transaction_tags_limit_is_shared_across_transactions(session_factory):
    """Wrong implementation: a plain ``limit`` buckets per concrete path, so
    every transaction id would get its own budget."""
    seeds = await _seed(session_factory)
    tx_a, tx_b = seeds["tx_ids"][:2]
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        for i in range(60):
            tx = tx_a if i % 2 else tx_b
            res = client.put(f"/api/v1/transactions/{tx}/tags", json={"tag_names": [f"x{i % 3}"]})
            assert res.status_code == 200, (i, res.text)
        res = client.put(f"/api/v1/transactions/{tx_a}/tags", json={"tag_names": ["x0"]})
    assert res.status_code == 429, res.text


# ── one budget of new tags, whatever route creates them ──────────────────────


async def _tag_count(factory, org_id: int) -> int:
    async with factory() as db:
        rows = await db.execute(select(Tag.id).where(Tag.org_id == org_id))
        return len(rows.all())


async def test_bulk_update_cannot_create_tags_past_the_budget(session_factory):
    """Wrong implementation: the limit on POST /tags only, while the bulk
    update creates tags on its own. Each request adds five new names."""
    seeds = await _seed(session_factory)
    tx_ids = seeds["tx_ids"]
    limit = tag_service.TAG_CREATE_LIMIT
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        # A transaction holds at most five tags, so each batch uses its own.
        for batch in range(limit // 5):
            res = client.post("/api/v1/transactions/bulk-update", json={
                "ids": [tx_ids[batch]], "tags": [f"b{batch}-{i}" for i in range(5)],
            })
            assert res.status_code == 200 and res.json()["skipped"] == [], res.text
        assert await _tag_count(session_factory, seeds["org_id"]) == limit
        rest = tx_ids[limit // 5:]
        res = client.post("/api/v1/transactions/bulk-update", json={
            "ids": rest, "tags": ["past-1", "past-2"],
        })
    assert res.status_code == 200, res.text
    skipped = res.json()["skipped"]
    assert [s["id"] for s in skipped] == rest
    assert "Too many new tags" in skipped[0]["reason"]
    assert await _tag_count(session_factory, seeds["org_id"]) == limit


async def test_transaction_tags_route_counts_tags_not_requests(session_factory):
    """Six PUTs of five new names each spend the 30-tag budget well under
    the route's 60-request limit; the next new name answers 429."""
    seeds = await _seed(session_factory)
    tx = seeds["tx_ids"][0]
    with TestClient(_app(session_factory, seeds["user_id"])) as client:
        for batch in range(tag_service.TAG_CREATE_LIMIT // 5):
            res = client.put(f"/api/v1/transactions/{tx}/tags",
                             json={"tag_names": [f"p{batch}-{i}" for i in range(5)]})
            assert res.status_code == 200, res.text
        res = client.put(f"/api/v1/transactions/{tx}/tags", json={"tag_names": ["one-more"]})
        assert res.status_code == 429, res.text
        assert int(res.headers["Retry-After"]) >= 1
        # Existing tags still attach: nothing new is created.
        res = client.put(f"/api/v1/transactions/{tx}/tags", json={"tag_names": ["p0-0"]})
        assert res.status_code == 200, res.text
        res = client.post("/api/v1/tags", json={"name": "via-post"})
    assert res.status_code == 429, res.text
