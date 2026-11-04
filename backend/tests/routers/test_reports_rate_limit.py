"""TBD-669: the report queries are limited per user, and the agent's query
tools draw the same bucket.

Real ``app.main`` app (the limiter, its 429 handler and the override loader
are wired there), sqlite for ``get_db`` / ``get_session_factory``, real
``get_current_user``. Every TestClient request comes from one address
("testclient"), so a second user getting a full budget proves the bucket is
the user. An org-level override lowers both limits to 2: that the agent is
held to it proves the override is read at call time.
"""
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import rate_limit_db
from app.agent.registry import ToolError, invoke
from app.database import get_db
from app.deps import get_session_factory
from app.main import app
from app.models import Base
from app.models.api_token import ApiToken
from app.models.feature_override import OrgFeatureOverride
from app.models.rate_limit_override import RateLimitOverride
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token

LIMIT = 2
QUERY = {"dataset": "transactions", "measure": {"agg": "sum", "field": "amount"}}
ROUTES = [("/api/v1/reports/query", "reports_query", QUERY),
          ("/api/v1/reports/query/sankey", "reports_sankey_query", {})]


@pytest.fixture(autouse=True)
def _fresh_limits():
    limiter.reset()
    rate_limit_db.reset()
    yield
    limiter.reset()
    rate_limit_db.reset()


@pytest.fixture
async def factory():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
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


async def _world(f, *, override=True) -> dict:
    async with f() as s:
        org = Organization(name=f"org-{uuid4().hex[:8]}", billing_cycle_day=1, primary_currency="EUR")
        s.add(org)
        await s.flush()
        users = [User(org_id=org.id, username=n, email=f"{n}@example.com", password_hash="x",
                      role=Role.MEMBER, is_active=True, last_active_at=datetime.now(timezone.utc))
                 for n in ("alice", "bob")]
        s.add_all([*users, OrgSetting(org_id=org.id, key="feature.reports", value="on"),
                   OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True)])
        if override:
            s.add_all([RateLimitOverride(org_id=org.id, endpoint_pattern=p, max_requests=LIMIT,
                                         period_seconds=60)
                       for p in ("reports.query", "reports.sankey_query")])
        await s.flush()
        tokens = [ApiToken(token_hash=secrets.token_hex(32), token_prefix=f"pat_{n}", name=f"t{n}",
                           scope="agent:read", created_by_user_id=users[0].id, created_by_email="a@x.example",
                           expires_at=datetime.utcnow() + timedelta(days=30)) for n in (1, 2)]
        s.add_all(tokens)
        await s.commit()
        return {"alice": users[0], "bob": users[1], "t1": tokens[0].id, "t2": tokens[1].id}


def _jwt(u: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(u.id, u.org_id, u.role.value)}"}


def _admitted(client: TestClient, u: User, url: str, body: dict, *, cap: int = 4) -> int:
    for n in range(cap):
        r = client.post(url, headers=_jwt(u), json=body)
        if r.status_code == 429:
            return n
        assert r.status_code == 200, (r.status_code, r.text)
    return cap


async def _agent(f, u: User, tool: str, body: dict, token: int | None = None) -> str:
    """``ok``, or the refusal code."""
    kw = {"channel": "mcp", "scope": "agent:read", "api_token_id": token} if token else {"channel": "in_app"}
    async with f() as db:
        user = await db.get(User, u.id)
        try:
            await invoke(db, user, tool, body, **kw)
        except ToolError as exc:
            return exc.code
    return "ok"


@pytest.mark.parametrize("url,tool,body", ROUTES, ids=["query", "sankey"])
async def test_rest_budget_is_per_user_not_per_address(factory, url, tool, body):
    w = await _world(factory)
    client = TestClient(app)  # no ``with``: skip the lifespan
    assert _admitted(client, w["alice"], url, body) == LIMIT
    assert _admitted(client, w["bob"], url, body) == LIMIT  # same address, a fresh budget


@pytest.mark.parametrize("url,tool,body", ROUTES, ids=["query", "sankey"])
async def test_an_override_tightens_the_agent_limit(factory, url, tool, body):
    """FENCE. Wrong implementation: the route's default (60/minute) read once
    at import, so an operator's override never reaches the agent."""
    w = await _world(factory)
    codes = [await _agent(factory, w["alice"], tool, body) for _ in range(LIMIT + 1)]
    assert codes == ["ok"] * LIMIT + ["route_rate_limited"]


async def test_two_tokens_of_one_user_share_one_bucket(factory):
    """FENCE. Wrong implementation: a bucket per agent token, so a user's
    second token is a second allowance."""
    w = await _world(factory)
    a = w["alice"]
    assert [await _agent(factory, a, "reports_query", QUERY, w["t1"]) for _ in range(LIMIT)] == ["ok"] * LIMIT
    assert await _agent(factory, a, "reports_query", QUERY, w["t2"]) == "route_rate_limited"
    assert await _agent(factory, w["bob"], "reports_query", QUERY) == "ok"  # another user's own bucket


@pytest.mark.parametrize("url,tool,body", ROUTES, ids=["query", "sankey"])
async def test_the_agent_and_rest_draw_one_bucket(factory, url, tool, body):
    """FENCE. Wrong implementation: the agent kept in a bucket of its own,
    a second allowance beside the browser's."""
    w = await _world(factory)
    client = TestClient(app)
    assert await _agent(factory, w["alice"], tool, body) == "ok"
    assert _admitted(client, w["alice"], url, body) == LIMIT - 1
    assert await _agent(factory, w["alice"], tool, body) == "route_rate_limited"


async def test_without_an_override_the_default_applies(factory):
    w = await _world(factory, override=False)
    codes = {await _agent(factory, w["alice"], "reports_query", QUERY) for _ in range(LIMIT + 1)}
    assert codes == {"ok"}
