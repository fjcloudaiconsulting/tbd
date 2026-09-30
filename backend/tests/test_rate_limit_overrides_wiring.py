"""Fences for TBD-492: admin rate-limit overrides are honoured by the limiter.

Real ``app.main`` app, sqlite for ``get_db`` / ``get_session_factory``, real
``get_current_user`` (JWT and PAT). ``_allowed`` counts the 200s before the
first 429, so each fence pins an exact number that differs from the static
default (5/hour on ``users.update_profile``).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_session_factory
from app.main import app
from app.models import Base
from app.models.api_token import ApiToken
from app.models.rate_limit_override import RateLimitOverride
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token
from app.services.api_token_service import hash_api_token

PATTERN = "users.update_profile"  # default 5/hour
DEFAULT = 5


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _reset_limiter():
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


async def _org(f, name: str) -> Organization:
    """Org ids run ahead of user ids (each call adds a filler org), so a loader
    that swaps user_id / org_id can never match by coincidence."""
    async with f() as s:
        for _ in range(3):
            s.add(Organization(name=f"filler-{uuid4().hex[:8]}", billing_cycle_day=1))
        await s.flush()
        o = Organization(name=name, billing_cycle_day=1)
        s.add(o)
        await s.commit()
        await s.refresh(o)
        return o


async def _user(f, org: Organization, name: str, *, superadmin: bool = False) -> User:
    async with f() as s:
        u = User(
            org_id=org.id,
            username=name,
            email=f"{name}@example.com",
            password_hash="x",
            role=Role.OWNER,
            is_superadmin=superadmin,
            is_active=True,
            last_active_at=datetime.now(timezone.utc),
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


async def _row(
    f,
    *,
    user: User | None = None,
    org: Organization | None = None,
    max_requests: int,
    period_seconds: int = 3600,
    expires_at: datetime | None = None,
    pattern: str = PATTERN,
) -> None:
    async with f() as s:
        s.add(
            RateLimitOverride(
                user_id=user.id if user else None,
                org_id=org.id if org else None,
                endpoint_pattern=pattern,
                max_requests=max_requests,
                period_seconds=period_seconds,
                expires_at=expires_at,
            )
        )
        await s.commit()


def _jwt(u: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(u.id, u.org_id, u.role.value)}"}


def _allowed(headers: dict, *, cap: int = 12, method: str = "put", url: str = "/api/v1/users/me",
             json=None) -> int:
    """200s before the first 429 (fresh counter, fresh client)."""
    limiter.reset()
    json = {} if json is None else json
    n = 0
    c = TestClient(app)  # no ``with``: skip the lifespan (dev migrations)
    for _ in range(cap):
        r = getattr(c, method)(url, headers=headers, json=json)
        if r.status_code == 429:
            return n
        assert r.status_code in (200, 201), (r.status_code, r.text)
        n += 1
    return n


async def test_f1_user_override_is_enforced(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    assert u.id != org.id
    await _row(factory, user=u, max_requests=2)
    assert _allowed(_jwt(u)) == 2


async def test_f1b_override_can_raise_the_limit(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=7)
    assert _allowed(_jwt(u)) == 7


async def test_f1c_no_row_keeps_the_default(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    assert _allowed(_jwt(u)) == DEFAULT


async def test_f2_pat_caller_honours_its_override(factory):
    org = await _org(factory, "a")
    su = await _user(factory, org, "root", superadmin=True)
    await _row(factory, user=su, max_requests=2, pattern="feedback.submit")
    plaintext = "pat_" + "a" * 43
    async with factory() as s:
        s.add(
            ApiToken(
                token_hash=hash_api_token(plaintext),
                token_prefix=plaintext[:14],
                name="t",
                scope="write",
                created_by_user_id=su.id,
                created_by_email=su.email,
                expires_at=_now() + timedelta(days=30),
            )
        )
        await s.commit()
    body = {"message": "hello", "category": "bug"}
    assert _allowed(
        {"Authorization": f"Bearer {plaintext}"},
        method="post", url="/api/v1/feedback", json=body,
    ) == 2


async def test_f3_non_standard_period_is_enforced(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=2, period_seconds=45)
    assert _allowed(_jwt(u)) == 2


async def test_f4_user_beats_org_in_both_insert_orders(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, org=org, max_requests=3)
    await _row(factory, user=u, max_requests=7)
    assert _allowed(_jwt(u)) == 7


async def test_f4_user_beats_org_user_row_first(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=7)
    await _row(factory, org=org, max_requests=3)
    assert _allowed(_jwt(u)) == 7


async def test_f4_org_row_applies_to_org_members(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, org=org, max_requests=3)
    assert _allowed(_jwt(u)) == 3


async def test_f4_user_beats_org_when_user_value_is_smaller(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=3)
    await _row(factory, org=org, max_requests=7)
    assert _allowed(_jwt(u)) == 3


async def test_f4_newest_user_row_wins(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=2)
    await _row(factory, user=u, max_requests=7)
    assert _allowed(_jwt(u)) == 7


async def test_f4_newest_user_row_wins_when_it_is_the_smaller(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=7)
    await _row(factory, user=u, max_requests=2)
    assert _allowed(_jwt(u)) == 2


async def test_f4_expired_row_is_ignored_future_row_applies(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=2, expires_at=_now() - timedelta(minutes=1))
    assert _allowed(_jwt(u)) == DEFAULT
    await _row(factory, user=u, max_requests=3, expires_at=_now() + timedelta(hours=1))
    assert _allowed(_jwt(u)) == 3


async def test_f4_another_orgs_and_users_rows_have_no_effect(factory):
    org_a = await _org(factory, "a")
    org_b = await _org(factory, "b")
    a = await _user(factory, org_a, "ua")
    b = await _user(factory, org_b, "ub")
    await _row(factory, org=org_b, max_requests=2)
    await _row(factory, user=b, max_requests=2)
    # Ids that collide with the tested identity across the two columns: a row
    # for org_id == a.id and one for user_id == org_a.id (neither is a's).
    assert a.id != org_a.id
    # Both colliding ids must be REAL rows: FKs are enforced on some runs.
    collide = await _user(factory, org_b, "c0")
    while collide.id < org_a.id:
        collide = await _user(factory, org_b, f"c{collide.id}")
    assert collide.id == org_a.id
    await _row(factory, org=SimpleNamespace(id=a.id), max_requests=2)  # a filler org
    await _row(factory, user=collide, max_requests=2)
    assert _allowed(_jwt(a)) == DEFAULT


async def test_f4_rows_for_other_patterns_have_no_effect(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=2, pattern="feedback.submit")
    assert _allowed(_jwt(u)) == DEFAULT


async def test_loader_ignores_unusable_sql_written_rows(factory):
    org = await _org(factory, "a")
    u = await _user(factory, org, "u1")
    await _row(factory, user=u, max_requests=0)
    await _row(factory, org=org, max_requests=4, period_seconds=0)
    assert _allowed(_jwt(u)) == DEFAULT


# ── F5: every dynamic route carries the loader, and the sets line up ────────


def _dynamic_groups():
    from fastapi.routing import APIRoute

    out = []  # (route, group)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        name = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
        for g in limiter._dynamic_route_limits.get(name, []):
            out.append((route, g))
    return out


def _dep_calls(dependant):
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _dep_calls(sub)


def test_f5_every_dynamic_route_loads_overrides_and_sets_line_up():
    import inspect

    from app.rate_limit_endpoint_catalogue import OVERRIDABLE_ENDPOINT_PATTERNS
    from app.rate_limit_overrides import load_rate_limit_overrides

    assert inspect.iscoroutinefunction(load_rate_limit_overrides)
    groups = _dynamic_groups()
    assert groups
    seen = set()
    for route, g in groups:
        provider = g._LimitGroup__limit_provider
        assert provider.pattern in OVERRIDABLE_ENDPOINT_PATTERNS, provider.pattern
        seen.add(provider.pattern)
        assert load_rate_limit_overrides in list(_dep_calls(route.dependant)), route.path
    assert seen == set(OVERRIDABLE_ENDPOINT_PATTERNS), seen ^ OVERRIDABLE_ENDPOINT_PATTERNS

    # No OVERRIDABLE pattern may still sit on a static (never-consulted) limit.
    from tests.test_rate_limit_catalogue_drift import DECORATOR_PATTERNS

    static = []
    for name in limiter._route_limits:
        mod, func = name.rsplit(".", 2)[-2:]
        pattern = DECORATOR_PATTERNS.get((mod, func), (None,))[0]
        if pattern in OVERRIDABLE_ENDPOINT_PATTERNS:
            static.append(name)
    assert not static, static
