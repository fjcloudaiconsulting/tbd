"""TBD-726: ``simulate`` and ``compare`` are rate limited per user.

Real ``app.main`` app (the limiter, its 429 handler and the override loader
are all wired there), sqlite for ``get_db`` / ``get_session_factory``, real
``get_current_user``. Every TestClient request comes from the same address
("testclient"), so a second user getting a full budget after the first one
is spent proves the bucket is the user, not the IP.

An org-level override lowers both limits to 2 so the fences stay fast; the
defaults themselves are pinned by ``tests/test_rate_limit_catalogue_drift.py``.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.main import app
from app.models import Base
from app.models.rate_limit_override import RateLimitOverride
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token

LIMIT = 2


@pytest.fixture(autouse=True)
def _plans_on_and_fresh_limiter(monkeypatch):
    monkeypatch.setattr(app_settings, "feature_plans", True)
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def factory():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _db():
        async with f() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: f
    yield f
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_session_factory, None)
    await eng.dispose()


async def _org_with_two_users(f) -> tuple[Organization, User, User]:
    async with f() as s:
        org = Organization(name=f"org-{uuid4().hex[:8]}", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        users = [
            User(
                org_id=org.id,
                username=name,
                email=f"{name}@example.com",
                password_hash="x",
                role=Role.OWNER if name == "alice" else Role.MEMBER,
                is_superadmin=False,
                is_active=True,
                last_active_at=datetime.now(timezone.utc),
            )
            for name in ("alice", "bob")
        ]
        s.add_all(users)
        for pattern in ("scenarios.simulate", "scenarios.compare"):
            s.add(
                RateLimitOverride(
                    org_id=org.id,
                    endpoint_pattern=pattern,
                    max_requests=LIMIT,
                    period_seconds=60,
                )
            )
        await s.commit()
        for u in users:
            await s.refresh(u)
        await s.refresh(org)
        return org, users[0], users[1]


def _jwt(u: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(u.id, u.org_id, u.role.value)}"}


def _admitted(client: TestClient, headers: dict, url: str, json: dict, *, cap: int = 6) -> int:
    """Requests that reach the handler before the first 429.

    The scenario ids do not exist, so an admitted request answers 404; the
    limiter counts it all the same.
    """
    n = 0
    for _ in range(cap):
        r = client.post(url, headers=headers, json=json)
        if r.status_code == 429:
            return n
        assert r.status_code == 404, (r.status_code, r.text)
        n += 1
    return n


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/api/v1/scenarios/999999/simulate", {"engine": "analytic", "options": {}}),
        ("/api/v1/scenarios/compare", {"scenario_ids": [999999], "horizon_months": 12}),
    ],
    ids=["simulate", "compare"],
)
async def test_budget_is_per_user_not_per_address(factory, url, body):
    _org, alice, bob = await _org_with_two_users(factory)
    client = TestClient(app)  # no ``with``: skip the lifespan (dev migrations)
    assert _admitted(client, _jwt(alice), url, body) == LIMIT
    # Same address, different user: a fresh budget.
    assert _admitted(client, _jwt(bob), url, body) == LIMIT


async def test_simulate_budget_is_shared_across_scenario_ids(factory):
    """``{scenario_id}`` is a path parameter: varying it must not mint a new
    budget (``shared_limit`` pins the bucket to the route)."""
    _org, alice, _bob = await _org_with_two_users(factory)
    client = TestClient(app)
    body = {"engine": "analytic", "options": {}}
    statuses = [
        client.post(f"/api/v1/scenarios/{sid}/simulate", headers=_jwt(alice), json=body).status_code
        for sid in (999001, 999002, 999003)
    ]
    assert statuses == [404, 404, 429]
